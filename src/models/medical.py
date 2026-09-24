from pydantic import BaseModel, Field
from typing import List, Optional
from decimal import Decimal


class ExtractedLineItem(BaseModel):
    description: str
    quantity: int = 1
    amount: Decimal


class ExtractedBill(BaseModel):
    hospital_name: Optional[str] = None
    gstin: Optional[str] = None
    bill_number: Optional[str] = None
    patient_name: Optional[str] = None
    total_amount: Optional[Decimal] = None
    line_items: List[ExtractedLineItem] = Field(default_factory=list)


class ExtractedPrescription(BaseModel):
    doctor_name: Optional[str] = None
    registration_number: Optional[str] = None
    patient_name: Optional[str] = None
    diagnoses: List[str] = Field(default_factory=list)
    medicines: List[str] = Field(default_factory=list)
    tests_ordered: List[str] = Field(default_factory=list)


class DocumentExtraction(BaseModel):
    file_id: str
    file_type: str
    confidence_score: float = Field(ge=0.0, le=1.0)
    prescription_data: Optional[ExtractedPrescription] = None
    bill_data: Optional[ExtractedBill] = None
    # e.g. "UNREADABLE", "EXTRACTION_FAILED" — non-fatal per-document quality
    # signals. A flagged document does not abort the whole batch; the
    # orchestrator decides what to do with the flag (see component contracts).
    flags: List[str] = Field(default_factory=list)

    def patient_name(self) -> Optional[str]:
        if self.prescription_data and self.prescription_data.patient_name:
            return self.prescription_data.patient_name
        if self.bill_data and self.bill_data.patient_name:
            return self.bill_data.patient_name
        return None
