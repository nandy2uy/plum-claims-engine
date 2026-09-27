"""Structured output of document extraction.

These models are the contract between the extraction layer (LLM or fixture) and
every downstream agent. They are deliberately tolerant of how real documents
and real model output look — "₹1,500/-", "01-Nov-2024", a single diagnosis
string instead of a list, `total` instead of `total_amount` — so that a
formatting quirk degrades one field instead of failing the whole document
(v1 raised a ValidationError on "₹1,000", which the pipeline then reported to
the member as "system error, please re-upload").
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Annotated, List, Literal, Optional

from pydantic import AliasChoices, BaseModel, BeforeValidator, ConfigDict, Field, model_validator

from src.core.text import parse_date_lenient, parse_money

PRESCRIPTION_TYPES = {"PRESCRIPTION"}
BILL_TYPES = {"HOSPITAL_BILL", "PHARMACY_BILL"}
REPORT_TYPES = {"LAB_REPORT", "DIAGNOSTIC_REPORT", "DENTAL_REPORT", "DISCHARGE_SUMMARY"}
KNOWN_DOCUMENT_TYPES = tuple(sorted(PRESCRIPTION_TYPES | BILL_TYPES | REPORT_TYPES))

# Flags an extraction can carry. MEMBER-fault flags ask the member to act;
# SYSTEM-fault flags degrade the pipeline; the rest feed fraud / confidence.
FLAG_UNREADABLE = "UNREADABLE"
FLAG_NO_CONTENT = "NO_CONTENT_PROVIDED"
FLAG_UNSUPPORTED_FILE = "UNSUPPORTED_FILE_TYPE"
FLAG_EXTRACTION_FAILED = "EXTRACTION_FAILED"
FLAG_TYPE_MISMATCH = "TYPE_MISMATCH"
FLAG_MISSING_CRITICAL_FIELDS = "MISSING_CRITICAL_FIELDS"
FLAG_ALTERATION = "DOCUMENT_ALTERATION"
FLAG_DUPLICATE_STAMP = "DUPLICATE_STAMP"


def _money(v):
    parsed = parse_money(v)
    if parsed is None:
        raise ValueError("amount is required")
    return parsed


def _str_list(v):
    if v is None:
        return []
    if isinstance(v, str):
        return [v] if v.strip() else []
    return [str(x) for x in v if x is not None and str(x).strip()]


def _quantity(v):
    try:
        return max(1, int(float(v)))
    except (TypeError, ValueError):
        return 1


Money = Annotated[Decimal, BeforeValidator(_money)]
OptMoney = Annotated[Optional[Decimal], BeforeValidator(parse_money)]
LenientDate = Annotated[Optional[date], BeforeValidator(parse_date_lenient)]
StrList = Annotated[List[str], BeforeValidator(_str_list)]


class _Tolerant(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class ExtractedLineItem(_Tolerant):
    description: str
    quantity: Annotated[int, BeforeValidator(_quantity)] = 1
    amount: Money
    is_branded: Optional[bool] = None


class ExtractedBill(_Tolerant):
    """`dropped_line_items` counts lines the source listed but whose amount could not be read.
    They are dropped (and flagged downstream) instead of failing the whole bill."""

    hospital_name: Optional[str] = Field(default=None, validation_alias=AliasChoices("hospital_name", "pharmacy_name", "clinic_name"))
    gstin: Optional[str] = None
    bill_number: Optional[str] = None
    date: LenientDate = Field(default=None, validation_alias=AliasChoices("date", "bill_date"))
    patient_name: Optional[str] = None
    total_amount: OptMoney = Field(default=None, validation_alias=AliasChoices("total_amount", "total", "net_amount"))
    amount_in_words: Optional[str] = None
    line_items: List[ExtractedLineItem] = Field(default_factory=list)
    dropped_line_items: int = 0

    @model_validator(mode="before")
    @classmethod
    def _drop_unreadable_lines(cls, data):
        if not isinstance(data, dict) or not isinstance(data.get("line_items"), list):
            return data
        kept, dropped = [], 0
        for item in data["line_items"]:
            try:
                ok = isinstance(item, dict) and bool(str(item.get("description") or "").strip()) \
                    and parse_money(item.get("amount")) is not None
            except ValueError:
                ok = False
            if ok:
                kept.append(item)
            else:
                dropped += 1
        return {**data, "line_items": kept, "dropped_line_items": int(data.get("dropped_line_items") or 0) + dropped}


class ExtractedPrescription(_Tolerant):
    doctor_name: Optional[str] = None
    registration_number: Optional[str] = Field(default=None, validation_alias=AliasChoices("registration_number", "doctor_registration"))
    specialization: Optional[str] = None
    clinic_name: Optional[str] = Field(default=None, validation_alias=AliasChoices("clinic_name", "hospital_name"))
    patient_name: Optional[str] = None
    date: LenientDate = None
    diagnoses: StrList = Field(default_factory=list, validation_alias=AliasChoices("diagnoses", "diagnosis"))
    medicines: StrList = Field(default_factory=list)
    tests_ordered: StrList = Field(default_factory=list, validation_alias=AliasChoices("tests_ordered", "investigations"))
    treatments: StrList = Field(default_factory=list, validation_alias=AliasChoices("treatments", "treatment"))
    # Closed-vocabulary clinical codes PROPOSED by the vision model (policy condition / exclusion names).
    # Advisory only: the policy engine never auto-rejects on a model tag alone.
    condition_tags: StrList = Field(default_factory=list)


class ExtractedReport(_Tolerant):
    lab_name: Optional[str] = None
    patient_name: Optional[str] = None
    date: LenientDate = Field(default=None, validation_alias=AliasChoices("date", "report_date", "sample_date"))
    tests: StrList = Field(default_factory=list, validation_alias=AliasChoices("tests", "test_name", "test_names"))
    remarks: Optional[str] = None
    pathologist_name: Optional[str] = None


Readability = Literal["GOOD", "POOR", "UNREADABLE"]
FailureKind = Literal["MEMBER", "SYSTEM"]


class DocumentExtraction(BaseModel):
    file_id: str
    file_name: Optional[str] = None
    declared_type: str
    detected_type: Optional[str] = None
    source: Literal["fixture", "llm", "none"] = "none"
    readability: Readability = "GOOD"
    confidence_score: float = Field(default=0.0, ge=0.0, le=1.0)
    prescription_data: Optional[ExtractedPrescription] = None
    bill_data: Optional[ExtractedBill] = None
    report_data: Optional[ExtractedReport] = None
    flags: List[str] = Field(default_factory=list)
    unreadable_fields: List[str] = Field(default_factory=list)
    error: Optional[str] = None
    # MEMBER: the member must fix it (blurry photo, wrong file). SYSTEM: our
    # fault (LLM down, key missing) — never blamed on the member.
    failure_kind: Optional[FailureKind] = None

    @property
    def effective_type(self) -> str:
        return self.detected_type or self.declared_type

    @property
    def usable(self) -> bool:
        return self.failure_kind is None

    @property
    def label(self) -> str:
        return self.file_name or self.file_id

    def patient_name(self) -> Optional[str]:
        for part in (self.prescription_data, self.bill_data, self.report_data):
            if part is not None and part.patient_name:
                return part.patient_name
        return None

    def document_date(self) -> Optional[date]:
        for part in (self.bill_data, self.prescription_data, self.report_data):
            if part is not None and part.date:
                return part.date
        return None
