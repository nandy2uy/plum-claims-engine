"""The mock-document generator and the field comparison used by scripts/run_live_samples.py."""

import json

from src.evals.samples import SAMPLES, compare, render_sample, write_samples


def test_every_sample_renders(tmp_path):
    paths = write_samples(tmp_path)
    assert len(paths) == len(SAMPLES)
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert {m["file_name"] for m in manifest} == {s.file_name for s in SAMPLES}
    pdf = next(s for s in SAMPLES if s.pages)
    assert render_sample(pdf)[:5] == b"%PDF-"


def test_two_page_pdf_renders_to_two_model_pages():
    import base64
    from src.core.llm import prepare_images
    from src.models.claim import DocumentSource
    pdf = next(s for s in SAMPLES if s.pages)
    doc = DocumentSource(file_id="p", file_type="HOSPITAL_BILL", mime_type="application/pdf",
                         content_base64=base64.b64encode(render_sample(pdf)).decode())
    assert len(prepare_images(doc, 10**7, 5)) == 2


def test_compare_is_tolerant_of_formatting_but_not_of_wrong_values():
    expected = {"detected_document_type": "HOSPITAL_BILL", "patient_name": "Rajesh Kumar", "total_amount": 1500,
                "line_item_count": 2, "diagnoses": ["diabetes"], "quality_flags": ["DOCUMENT_ALTERATION"]}
    good = {"detected_document_type": "HOSPITAL_BILL", "quality_flags": ["DOCUMENT_ALTERATION"],
            "bill": {"patient_name": "Mr. RAJESH  KUMAR", "total_amount": "1500.00", "line_items": [{}, {}]},
            "prescription": {"diagnoses": ["Type 2 Diabetes Mellitus (T2DM)"]}}
    assert all(c.ok for c in compare(expected, good))
    bad = {**good, "bill": {"patient_name": "Arjun Mehta", "total_amount": 1400, "line_items": [{}]}}
    failed = {c.field for c in compare(expected, bad) if not c.ok}
    assert failed == {"patient_name", "total_amount", "line_item_count"}
