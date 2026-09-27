"""Mock medical documents for live-extraction checks (sample_documents_guide.md formats).

Component contract
-------------------
SAMPLES: List[Sample]  each with file_name, declared_type, mime_type, the text lines to render,
                       quality variations to apply, and `expected` field values.
render_sample(sample) -> bytes       PNG (or PDF for multi-page samples), deterministic.
write_samples(out_dir) -> List[Path] writes every sample plus manifest.json (expected values).
compare(expected, extraction) -> List[FieldCheck]
                                     field-level comparison used by the live report: strings are
                                     compared normalised (case, spacing, punctuation), lists by
                                     "every expected item found", amounts numerically.
Errors: none (Pillow and pypdfium2 are required dependencies).

Variations mirror the guide's quality table: a clean print, a skewed low-contrast phone photo,
a handwritten-style prescription with shorthand (HTN, T2DM), a bill with a crossed-out and
rewritten amount, and a two-page scanned PDF bill whose line items must be aggregated.
"""

from __future__ import annotations

import io
import json
import random
import re
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional

from PIL import Image, ImageDraw, ImageFilter, ImageFont

WIDTH, HEIGHT = 900, 1150


@dataclass
class Sample:
    file_name: str
    declared_type: str
    title: str
    lines: List[str]
    expected: Dict[str, Any]
    variations: List[str] = field(default_factory=list)
    pages: Optional[List[List[str]]] = None  # multi-page PDF

    @property
    def mime_type(self) -> str:
        return "application/pdf" if self.pages else "image/png"


SAMPLES: List[Sample] = [
    Sample("01_clean_prescription.png", "PRESCRIPTION", "Dr. Arun Sharma, MBBS, MD (Internal Medicine)", [
        "Reg. No: KA/45678/2015", "City Medical Centre, 12 MG Road, Bengaluru", "",
        "Patient: Rajesh Kumar          Date: 01-Nov-2024", "Age: 39 years   Gender: M", "",
        "Diagnosis: Viral Fever", "", "Rx:", "1. Tab Paracetamol 650mg - 1-1-1 x 5 days",
        "2. Tab Vitamin C 500mg - 0-0-1 x 7 days", "", "Investigations: CBC, Dengue NS1",
        "", "                                  [Signature]"],
        {"detected_document_type": "PRESCRIPTION", "patient_name": "Rajesh Kumar", "doctor_name": "Arun Sharma",
         "registration_number": "KA/45678/2015", "date": "2024-11-01", "diagnoses": ["Viral Fever"],
         "tests_ordered": ["CBC", "Dengue NS1"]}),
    Sample("02_clean_hospital_bill.png", "HOSPITAL_BILL", "CITY MEDICAL CENTRE", [
        "12 MG Road, Bengaluru - 560001", "GSTIN: 29ABCDE1234F1Z5", "", "BILL / RECEIPT",
        "Bill No: CMC/2024/08321    Date: 01-Nov-2024", "Patient Name: Rajesh Kumar", "",
        "DESCRIPTION                         AMOUNT", "Consultation Fee (OPD)              1000.00",
        "CBC (Complete Blood Count)           200.00", "Dengue NS1 Antigen Test              300.00", "",
        "Total Amount:                       1500.00", "Rupees One Thousand Five Hundred Only"],
        {"detected_document_type": "HOSPITAL_BILL", "hospital_name": "City Medical Centre", "patient_name": "Rajesh Kumar",
         "bill_number": "CMC/2024/08321", "date": "2024-11-01", "total_amount": 1500, "line_item_count": 3}),
    Sample("03_phone_photo_bill.png", "HOSPITAL_BILL", "APOLLO HOSPITALS", [
        "Jayanagar, Bengaluru", "Bill No: AH/OPD/77120    Date: 03-Nov-2024", "Patient Name: Deepak Shah", "",
        "Consultation Fee                    1500.00", "Medicines                           3000.00", "",
        "Total Amount:                       4500.00"],
        {"detected_document_type": "HOSPITAL_BILL", "hospital_name": "Apollo Hospitals", "patient_name": "Deepak Shah",
         "date": "2024-11-03", "total_amount": 4500, "line_item_count": 2},
        variations=["skew", "blur", "low_contrast", "shadow"]),
    Sample("04_handwritten_prescription.png", "PRESCRIPTION", "Dr. Sunil Mehta", [
        "Reg No GJ/56789/2014", "", "Pt: Vikram Joshi     15/10/24", "", "c/o fatigue, polyuria",
        "Dx: T2DM, HTN", "", "Rx  Metformin 500 mg  1-0-1", "      Glimepiride 1 mg  1-0-0",
        "      Telmisartan 40 mg  0-0-1"],
        {"detected_document_type": "PRESCRIPTION", "patient_name": "Vikram Joshi", "registration_number": "GJ/56789/2014",
         "date": "2024-10-15", "diagnoses": ["diabetes", "hypertension"]},
        variations=["handwriting", "stamp"]),
    Sample("05_altered_pharmacy_bill.png", "PHARMACY_BILL", "HEALTH FIRST PHARMACY", [
        "Drug Lic. No: KA-BLR-2231", "Bill No: HFP-24-09821    Date: 01-Nov-2024", "Patient: Rajesh Kumar", "",
        "MEDICINE            QTY   AMT", "Paracetamol 650      15   37.50", "Vitamin C 500        10   40.00", "",
        "Net Amount:              [AMOUNT]"],
        {"detected_document_type": "PHARMACY_BILL", "patient_name": "Rajesh Kumar", "date": "2024-11-01",
         "line_item_count": 2, "quality_flags": ["DOCUMENT_ALTERATION"]},
        variations=["altered_amount"]),
    Sample("06_two_page_bill.pdf", "HOSPITAL_BILL", "MANIPAL HOSPITALS", [], {
        "detected_document_type": "HOSPITAL_BILL", "hospital_name": "Manipal Hospitals", "patient_name": "Priya Singh",
        "date": "2024-10-15", "total_amount": 2600, "line_item_count": 4},
        pages=[["Old Airport Road, Bengaluru", "Bill No: MH/24/5521    Date: 15-Oct-2024", "Patient Name: Priya Singh", "",
                "Consultation Fee                     900.00", "X-Ray Chest PA                       600.00",
                "", "(continued on page 2)"],
               ["Bill No: MH/24/5521 (page 2 of 2)", "", "Nebulisation                         300.00",
                "Medicines                            800.00", "", "Total Amount:                       2600.00"]]),
]


# ---------------------------------------------------------------- rendering

def _font(size: int):
    return ImageFont.load_default(size=size)


def _page(title: str, lines: List[str], variations: List[str], seed: int) -> Image.Image:
    rng = random.Random(seed)
    img = Image.new("RGB", (WIDTH, HEIGHT), "white")
    draw = ImageDraw.Draw(img)
    draw.rectangle([30, 30, WIDTH - 30, HEIGHT - 30], outline="black", width=2)
    draw.text((60, 60), title, fill="black", font=_font(30))
    y = 120
    handwriting = "handwriting" in variations
    for line in lines:
        if line == "Net Amount:              [AMOUNT]" and "altered_amount" in variations:
            draw.text((60, y), "Net Amount:", fill="black", font=_font(24))
            draw.text((420, y), "77.50", fill="black", font=_font(24))
            draw.line([410, y + 14, 500, y + 14], fill="black", width=3)          # crossed out
            draw.text((520, y - 4), "97.50", fill=(20, 40, 160), font=_font(28))  # rewritten in pen
        else:
            x = 60 + (rng.randint(-6, 10) if handwriting else 0)
            colour = (25, 45, 150) if handwriting else "black"
            draw.text((x, y), line, fill=colour, font=_font(27 if handwriting else 24))
        y += 44 if handwriting else 38
    if "stamp" in variations:
        draw.ellipse([520, 120, 780, 260], outline=(170, 30, 30), width=5)
        draw.text((565, 175), "REG. STAMP", fill=(170, 30, 30), font=_font(26))
    if "low_contrast" in variations:
        img = Image.blend(img, Image.new("RGB", img.size, (205, 200, 185)), 0.45)
    if "shadow" in variations:
        shade = Image.new("L", img.size, 0)
        ImageDraw.Draw(shade).rectangle([WIDTH // 2, 0, WIDTH, HEIGHT], fill=70)
        img = Image.composite(Image.new("RGB", img.size, (60, 60, 60)), img, shade)
    if "blur" in variations:
        img = img.filter(ImageFilter.GaussianBlur(1.2))
    if "skew" in variations:
        img = img.rotate(4, expand=True, fillcolor=(120, 110, 100))
    return img


def render_sample(sample: Sample) -> bytes:
    seed = sum(map(ord, sample.file_name))
    if sample.pages:
        images = [_page(sample.title if i == 0 else sample.title + " (contd.)", lines, sample.variations, seed + i)
                  for i, lines in enumerate(sample.pages)]
        buf = io.BytesIO()
        images[0].save(buf, format="PDF", save_all=True, append_images=images[1:], resolution=110)
        return buf.getvalue()
    buf = io.BytesIO()
    _page(sample.title, sample.lines, sample.variations, seed).save(buf, format="PNG")
    return buf.getvalue()


def write_samples(out_dir: Path) -> List[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for sample in SAMPLES:
        path = out_dir / sample.file_name
        path.write_bytes(render_sample(sample))
        written.append(path)
    manifest = [{"file_name": s.file_name, "declared_type": s.declared_type, "mime_type": s.mime_type,
                 "variations": s.variations, "expected": s.expected} for s in SAMPLES]
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return written


# ---------------------------------------------------------------- comparison

@dataclass
class FieldCheck:
    field: str
    expected: Any
    actual: Any
    ok: bool

    def as_dict(self) -> dict:
        return asdict(self)


def _norm(value: Any) -> str:
    text = re.sub(r"^(dr|mr|mrs|ms)\.?\s+", "", str(value or "").lower().strip())
    return re.sub(r"[^a-z0-9]", "", text)


def _find(extraction: dict, name: str) -> Any:
    for part in ("prescription", "bill", "report"):
        if isinstance(extraction.get(part), dict) and name in extraction[part]:
            return extraction[part][name]
    return extraction.get(name)


def compare(expected: Dict[str, Any], extraction: dict) -> List[FieldCheck]:
    """`extraction` is LLMDocumentResult.model_dump(mode="json")."""
    checks = []
    for name, want in expected.items():
        if name == "line_item_count":
            items = _find(extraction, "line_items") or []
            checks.append(FieldCheck(name, want, len(items), len(items) == want))
        elif name == "quality_flags":
            got = extraction.get("quality_flags") or []
            checks.append(FieldCheck(name, want, got, all(f in got for f in want)))
        elif name == "total_amount":
            got = _find(extraction, name)
            ok = got is not None and Decimal(str(got)) == Decimal(str(want))
            checks.append(FieldCheck(name, want, got, ok))
        elif isinstance(want, list):
            got = _find(extraction, name) or []
            haystack = " ".join(str(g).lower() for g in got)
            checks.append(FieldCheck(name, want, got, all(str(w).lower() in haystack for w in want)))
        elif name == "detected_document_type":
            got = extraction.get(name)
            checks.append(FieldCheck(name, want, got, got == want))
        else:
            got = _find(extraction, name)
            checks.append(FieldCheck(name, want, got, _norm(want) in _norm(got) or (_norm(got) and _norm(got) in _norm(want))))
    return checks
