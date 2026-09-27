"""Live document understanding with a vision model (Gemini by default, or OpenAI).

Component contract
-------------------
DocumentExtractorBackend (Protocol)
    async extract(doc: DocumentSource) -> LLMDocumentResult

VisionExtractor(settings, condition_vocabulary=[]) implements it with ONE vision call per document.
Both providers are reached through the OpenAI-compatible Chat Completions API with the `openai`
SDK: Gemini at https://generativelanguage.googleapis.com/v1beta/openai/ (settings.llm_provider =
"gemini", GEMINI_API_KEY), OpenAI at its default endpoint (llm_provider = "openai", OPENAI_API_KEY).
The call does three jobs at once:
  1. classify   -> detected_document_type (so a prescription uploaded as a
                   "hospital bill" is caught; v1 trusted the member's label)
  2. assess     -> readability GOOD/POOR/UNREADABLE, model confidence,
                   unreadable_fields, quality_flags (DOCUMENT_ALTERATION,
                   DUPLICATE_STAMP, STAMP_OBSCURES_TEXT, HANDWRITTEN, PARTIAL_DOCUMENT,
                   AMOUNT_WORDS_MISMATCH)
  3. extract    -> prescription / bill / report fields, plus `condition_tags`: clinical codes chosen
                   from a CLOSED vocabulary built from policy_terms.json (waiting-period conditions and
                   exclusion names). Tags outside the vocabulary are discarded after parsing. Tags are
                   advisory: the policy engine never rejects on a tag alone (see PolicyEngine).

Structured output: the request uses STRICT JSON-schema mode (STRICT_SCHEMA below: every field
required, nullable where the document may not show it, no extra keys), so the model cannot return an
off-schema object. Providers differ in what they accept, so on an HTTP 400 the client steps down
json_schema_strict -> json_object -> prompt-only JSON, remembers the first mode that works, and
records it as schema_mode.
Either way the output is validated against LLMDocumentResult (Pydantic), with one repair
round-trip feeding the validation error back to the model.

Reproducibility: every result carries `meta` (provider, model, prompt_version, schema_mode, latency_ms,
input/output tokens, attempts, repaired), which the extractor writes into the trace.

Inputs accepted: JPEG/PNG/WebP/GIF images (bytes or http(s) URL) and PDFs (bytes; pages rendered to
PNG with pypdfium2, a pip-only dependency with no poppler needed, capped at settings.max_pdf_pages,
all pages sent in the same call so multi-page bills are aggregated).

Errors: ExtractionError(kind=...)
  kind="INPUT"  : the member's file is the problem (unsupported type, corrupt PDF, too large).
                  The member is asked to upload a different file.
  kind="SYSTEM" : our problem (no API key, timeout after retries, auth error, unusable model
                  output). Never blamed on the member; the claim degrades to manual review.
Retries: only retryable failures (timeouts, connection errors, 429, 5xx) are retried, with
exponential backoff. The SDK's own retry loop is disabled (max_retries=0) so retries don't nest.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import io
import json
import logging
import time
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Dict, List, Literal, Optional, Protocol, Union

from pydantic import BaseModel, Field, ValidationError, field_validator

from src.core.config import llm_api_key, llm_base_url, llm_key_env, llm_model
from src.models.claim import DocumentSource
from src.models.medical import (
    KNOWN_DOCUMENT_TYPES, ExtractedBill, ExtractedPrescription, ExtractedReport, Readability, StrList,
)

logger = logging.getLogger("claims.llm")

PROMPT_VERSION = "extract-v3"
IMAGE_MIME_TYPES = {"image/jpeg", "image/jpg", "image/png", "image/webp", "image/gif"}
PDF_MIME_TYPES = {"application/pdf"}
RETRYABLE_ERRORS = {"APITimeoutError", "APIConnectionError", "RateLimitError", "InternalServerError", "TimeoutError"}
SCHEMA_MODES = ["json_schema_strict", "json_object", "prompt_only"]
QUALITY_FLAGS = ["DOCUMENT_ALTERATION", "DUPLICATE_STAMP", "STAMP_OBSCURES_TEXT", "HANDWRITTEN",
                 "PARTIAL_DOCUMENT", "MULTILINGUAL", "AMOUNT_WORDS_MISMATCH"]


class ExtractionError(Exception):
    def __init__(self, message: str, kind: Literal["SYSTEM", "INPUT"] = "SYSTEM"):
        super().__init__(message)
        self.kind = kind


class ExtractionMeta(BaseModel):
    """How a live extraction was produced; written into the trace for reproducibility."""

    provider: str = ""
    model: str = ""
    prompt_version: str = PROMPT_VERSION
    schema_mode: str = "json_schema_strict"
    latency_ms: int = 0
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    attempts: int = 0
    repaired: bool = False
    pages: int = 0


class LLMDocumentResult(BaseModel):
    detected_document_type: str = "OTHER"
    readability: Readability = "GOOD"
    confidence: float = 0.8
    unreadable_fields: StrList = Field(default_factory=list)
    quality_flags: StrList = Field(default_factory=list)
    prescription: Optional[ExtractedPrescription] = None
    bill: Optional[ExtractedBill] = None
    report: Optional[ExtractedReport] = None
    meta: ExtractionMeta = Field(default_factory=ExtractionMeta)

    @field_validator("detected_document_type", mode="before")
    @classmethod
    def _norm_type(cls, v):
        v = str(v or "OTHER").strip().upper().replace(" ", "_")
        return v if v in KNOWN_DOCUMENT_TYPES else "OTHER"

    @field_validator("readability", mode="before")
    @classmethod
    def _norm_readability(cls, v):
        v = str(v or "GOOD").strip().upper()
        return v if v in ("GOOD", "POOR", "UNREADABLE") else "POOR"

    @field_validator("confidence", mode="before")
    @classmethod
    def _clamp(cls, v):
        try:
            return min(1.0, max(0.0, float(v)))
        except (TypeError, ValueError):
            return 0.5


class DocumentExtractorBackend(Protocol):
    async def extract(self, doc: DocumentSource) -> LLMDocumentResult: ...


@dataclass
class Completion:
    """What one model call returned. `_complete` may also return a bare string (tests)."""

    content: str
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None


# ------------------------------------------------------------------ strict schema

def _nullable(schema: Dict[str, Any]) -> Dict[str, Any]:
    if "type" in schema and isinstance(schema["type"], str):
        return {**schema, "type": [schema["type"], "null"]}
    return {"anyOf": [schema, {"type": "null"}]}


def _obj(properties: Dict[str, Any]) -> Dict[str, Any]:
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


_STR = {"type": "string"}
_STR_LIST = {"type": "array", "items": {"type": "string"}}

STRICT_SCHEMA: Dict[str, Any] = _obj({
    "detected_document_type": {"type": "string", "enum": list(KNOWN_DOCUMENT_TYPES) + ["OTHER"]},
    "readability": {"type": "string", "enum": ["GOOD", "POOR", "UNREADABLE"]},
    "confidence": {"type": "number"},
    "unreadable_fields": _STR_LIST,
    "quality_flags": {"type": "array", "items": {"type": "string", "enum": QUALITY_FLAGS}},
    "prescription": _nullable(_obj({
        "doctor_name": _nullable(_STR), "registration_number": _nullable(_STR), "specialization": _nullable(_STR),
        "clinic_name": _nullable(_STR), "patient_name": _nullable(_STR), "date": _nullable(_STR),
        "diagnoses": _STR_LIST, "medicines": _STR_LIST, "tests_ordered": _STR_LIST, "treatments": _STR_LIST,
        "condition_tags": _STR_LIST,
    })),
    "bill": _nullable(_obj({
        "hospital_name": _nullable(_STR), "gstin": _nullable(_STR), "bill_number": _nullable(_STR),
        "date": _nullable(_STR), "patient_name": _nullable(_STR), "total_amount": _nullable({"type": "number"}),
        "amount_in_words": _nullable(_STR),
        "line_items": {"type": "array", "items": _obj({
            "description": _STR, "quantity": _nullable({"type": "integer"}),
            "amount": _nullable({"type": "number"}), "is_branded": _nullable({"type": "boolean"}),
        })},
    })),
    "report": _nullable(_obj({
        "lab_name": _nullable(_STR), "patient_name": _nullable(_STR), "date": _nullable(_STR),
        "tests": _STR_LIST, "remarks": _nullable(_STR), "pathologist_name": _nullable(_STR),
    })),
})

SYSTEM_PROMPT = """You are the document-understanding stage of an Indian health-insurance OPD claims system.
You receive photos or scans of medical documents. They are often handwritten, stamped over, skewed phone
photos, multi-page, or partly in Hindi/Tamil/Telugu.

Rules:
- Classify the document by what it IS, not by what the member said it is.
- Extract only what you can actually read. Never guess or invent a value: use null / [] and list the field
  name in "unreadable_fields" when it is present but illegible.
- readability: "UNREADABLE" if the key content (names, amounts, diagnosis) cannot be read; "POOR" if some
  fields are illegible; otherwise "GOOD". confidence: 0..1, how sure you are the extracted values are correct.
- Dates as YYYY-MM-DD. Amounts as plain rupee numbers (no symbols or commas). Line item "amount" is the line
  total. If a line's amount is illegible, keep the line with amount null.
- If the bill states the total in words, copy it to "amount_in_words"; if words and figures disagree, add the
  quality flag AMOUNT_WORDS_MISMATCH.
- Expand medical shorthand in diagnoses (HTN -> Hypertension, T2DM -> Type 2 Diabetes Mellitus, URI -> Upper
  Respiratory Infection) but keep the original in brackets.
- condition_tags: only values copied exactly from the condition vocabulary in the user message, and only for
  conditions the patient HAS per the diagnosis or treatment. Never tag a negated or family-history mention
  ("no diabetes", "family history of hypertension").
- Doctor registration numbers look like KA/45678/2015 (state/number/year) or AYUR/KL/2345/2019 (AYUSH).
  Copy them exactly as printed.
- Flag DOCUMENT_ALTERATION if amounts or dates are crossed out / overwritten, DUPLICATE_STAMP if it carries a
  DUPLICATE stamp or conflicting ORIGINAL/DUPLICATE stamps, STAMP_OBSCURES_TEXT if a stamp covers text.
- For pharmacy bills set is_branded=true for brand-name medicines, false for generics, null if unsure.
- Put regional-language fields you cannot translate in "unreadable_fields" rather than transliterating guesses.
- Respond with ONE JSON object only (no prose, no markdown fences) with exactly these top-level keys:
  detected_document_type, readability, confidence, unreadable_fields, quality_flags, prescription, bill, report.
  Use null for prescription/bill/report when the document is not that kind."""


def prepare_images(doc: DocumentSource, max_bytes: int, max_pdf_pages: int) -> List[str]:
    """Turn a DocumentSource into a list of image URLs (data: or http(s):) for the model."""
    if doc.content_base64:
        try:
            raw = base64.b64decode(doc.content_base64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ExtractionError(f"'{doc.label}' is not valid base64 content.", kind="INPUT") from exc
        if len(raw) > max_bytes:
            raise ExtractionError(
                f"'{doc.label}' is {len(raw) / 1e6:.1f} MB; the maximum is {max_bytes / 1e6:.0f} MB.", kind="INPUT")
        mime = (doc.mime_type or "").lower()
        if mime in PDF_MIME_TYPES or raw[:5] == b"%PDF-":
            return _render_pdf(raw, doc.label, max_pdf_pages)
        if mime in IMAGE_MIME_TYPES:
            return [f"data:{'image/jpeg' if mime == 'image/jpg' else mime};base64,{doc.content_base64}"]
        raise ExtractionError(
            f"'{doc.label}' is a {mime or 'unknown'} file. Please upload a JPG, PNG or PDF.", kind="INPUT")
    if doc.content_url:
        url = doc.content_url.strip()
        if not url.lower().startswith(("http://", "https://", "data:image/")):
            raise ExtractionError(f"'{doc.label}' has an unsupported content_url scheme.", kind="INPUT")
        if url.lower().split("?")[0].endswith(".pdf"):
            raise ExtractionError(f"'{doc.label}': PDF links are not supported — upload the PDF file itself.", kind="INPUT")
        return [url]
    raise ExtractionError(f"'{doc.label}' has no content.", kind="INPUT")


def _render_pdf(raw: bytes, label: str, max_pages: int) -> List[str]:
    try:
        import pypdfium2 as pdfium
    except ImportError as exc:  # pragma: no cover - dependency is in requirements.txt
        raise ExtractionError("PDF rendering requires pypdfium2, which is not installed.", kind="SYSTEM") from exc
    try:
        pdf = pdfium.PdfDocument(raw)
    except Exception as exc:  # pdfium raises its own PdfiumError
        raise ExtractionError(f"'{label}' could not be opened as a PDF (corrupt or password-protected).", kind="INPUT") from exc
    pages = []
    try:
        for index in range(min(len(pdf), max_pages)):
            image = pdf[index].render(scale=2).to_pil()
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
            pages.append("data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode())
    finally:
        pdf.close()
    if not pages:
        raise ExtractionError(f"'{label}' is an empty PDF.", kind="INPUT")
    return pages


@lru_cache(maxsize=4)
def _client(api_key: str, timeout: float, base_url: str):
    from openai import AsyncOpenAI

    return AsyncOpenAI(api_key=api_key, max_retries=0, timeout=timeout, base_url=base_url or None)


class VisionExtractor:
    def __init__(self, settings, condition_vocabulary: Optional[List[str]] = None):
        self.settings = settings
        self.vocabulary = list(dict.fromkeys(condition_vocabulary or []))
        self.mode_index = 0  # index into SCHEMA_MODES of the first mode known to work

    async def extract(self, doc: DocumentSource) -> LLMDocumentResult:
        images = prepare_images(doc, int(self.settings.max_upload_mb * 1_000_000), self.settings.max_pdf_pages)
        vocab = json.dumps(self.vocabulary) if self.vocabulary else "[] (leave condition_tags empty)"
        content = [{"type": "text", "text": (
            f"The member labelled this document as: {doc.file_type}. It has {len(images)} page image(s). "
            f"Return one JSON object with the document's type, quality and fields.\n"
            f"Condition vocabulary for prescription.condition_tags: {vocab}")}]
        content += [{"type": "image_url", "image_url": {"url": url}} for url in images]
        messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": content}]

        meta = ExtractionMeta(provider=self.settings.llm_provider, model=llm_model(self.settings), pages=len(images))
        started = time.perf_counter()
        raw = await self._complete_with_retry(messages, meta)
        try:
            result = self._restrict_tags(self._parse(raw))
        except (json.JSONDecodeError, ValidationError) as first_error:
            logger.warning("Extraction output for %s failed validation, attempting repair: %s", doc.label, first_error)
            meta.repaired = True
            messages += [
                {"role": "assistant", "content": raw},
                {"role": "user", "content": f"That output was invalid: {first_error}. Return the corrected JSON object only."},
            ]
            raw = await self._complete_with_retry(messages, meta)
            try:
                result = self._restrict_tags(self._parse(raw))
            except (json.JSONDecodeError, ValidationError) as exc:
                raise ExtractionError(f"Model output failed schema validation after one repair attempt: {exc}") from exc
        meta.latency_ms = int((time.perf_counter() - started) * 1000)
        result.meta = meta
        return result

    def _restrict_tags(self, result: LLMDocumentResult) -> LLMDocumentResult:
        """Closed vocabulary: anything the model invents outside it is dropped, case-insensitively matched."""
        if result.prescription is not None:
            allowed = {v.lower(): v for v in self.vocabulary}
            tags = [allowed[t.strip().lower()] for t in result.prescription.condition_tags if t.strip().lower() in allowed]
            result.prescription.condition_tags = list(dict.fromkeys(tags))
        return result

    @staticmethod
    def _parse(raw: str) -> LLMDocumentResult:
        text = raw.strip()
        if text.startswith("```"):
            text = text.strip("`").removeprefix("json").strip()
        data = json.loads(text)
        if isinstance(data, dict):
            data.pop("meta", None)  # meta is ours, never the model's
        return LLMDocumentResult.model_validate(data)

    async def _complete_with_retry(self, messages, meta: ExtractionMeta) -> str:
        if not llm_api_key(self.settings):
            raise ExtractionError(f"{llm_key_env(self.settings)} is not configured, so live document extraction "
                                  f"is unavailable.")
        last: Optional[Exception] = None
        for attempt in range(self.settings.llm_max_retries + 1):
            meta.attempts += 1
            try:
                completion = await self._complete_structured(messages, meta)
                if isinstance(completion, str):
                    completion = Completion(completion)
                if completion.input_tokens is not None:
                    meta.input_tokens = (meta.input_tokens or 0) + completion.input_tokens
                if completion.output_tokens is not None:
                    meta.output_tokens = (meta.output_tokens or 0) + completion.output_tokens
                return completion.content
            except Exception as exc:  # noqa: BLE001 - classified below
                name = type(exc).__name__
                if name not in RETRYABLE_ERRORS:
                    raise ExtractionError(f"Vision model call failed ({name}): {exc}") from exc
                last = exc
                if attempt < self.settings.llm_max_retries:
                    await asyncio.sleep(0.5 * (2 ** attempt))
        raise ExtractionError(f"Vision model unavailable after {self.settings.llm_max_retries + 1} attempts: {last}") from last

    async def _complete_structured(self, messages, meta: ExtractionMeta) -> Union[Completion, str]:
        """Try the strongest structured-output mode the endpoint has accepted so far. On HTTP 400 step
        down one mode and retry; the first mode that succeeds is remembered for later calls. If even
        prompt-only mode gets a 400, the request itself is bad (e.g. an image the provider refuses)."""
        last: Optional[Exception] = None
        for index in range(self.mode_index, len(SCHEMA_MODES)):
            mode = SCHEMA_MODES[index]
            meta.schema_mode = mode
            try:
                completion = await self._complete(messages, self._response_format(mode))
            except Exception as exc:  # noqa: BLE001
                if type(exc).__name__ != "BadRequestError":
                    raise
                logger.warning("%s rejected response mode %s: %s", self.settings.llm_provider, mode, exc)
                last = exc
                continue
            if index != self.mode_index:
                logger.warning("Using response mode %s for %s from now on.", mode, self.settings.llm_provider)
                self.mode_index = index
            return completion
        raise last  # type: ignore[misc]

    @staticmethod
    def _response_format(mode: str) -> Optional[Dict[str, Any]]:
        if mode == "json_schema_strict":
            return {"type": "json_schema",
                    "json_schema": {"name": "document_extraction", "strict": True, "schema": STRICT_SCHEMA}}
        if mode == "json_object":
            return {"type": "json_object"}
        return None

    async def _complete(self, messages, response_format: Optional[Dict[str, Any]]) -> Union[Completion, str]:
        s = self.settings
        client = _client(llm_api_key(s), s.llm_timeout_seconds, llm_base_url(s))
        kwargs: Dict[str, Any] = {"model": llm_model(s), "messages": messages, "temperature": 0}
        if response_format is not None:
            kwargs["response_format"] = response_format
        response = await asyncio.wait_for(client.chat.completions.create(**kwargs), timeout=s.llm_timeout_seconds)
        usage = getattr(response, "usage", None)
        return Completion(
            content=response.choices[0].message.content or "",
            input_tokens=getattr(usage, "prompt_tokens", None),
            output_tokens=getattr(usage, "completion_tokens", None),
        )


# Backwards-compatible name (the extractor was OpenAI-only in earlier versions).
OpenAIVisionExtractor = VisionExtractor
