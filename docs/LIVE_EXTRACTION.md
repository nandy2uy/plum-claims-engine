# Live extraction results

Generated 2026-09-27 08:17 UTC with `gemini` / `gemini-3.8-flash` by `python scripts/run_live_samples.py`. Documents are the mock files in `samples/` (rendered by `scripts/make_samples.py` following sample_documents_guide.md): a clean prescription and bill, a skewed and shadowed phone photo, a handwritten-style prescription with shorthand and a stamp, a pharmacy bill with a crossed-out amount, and a two-page scanned PDF.

**Field accuracy: 36/36 expected fields extracted correctly across 6 of 6 documents.**

| Document | Variations | Detected | Readability | Fields correct | Latency | Tokens in/out | Schema mode |
|---|---|---|---|---|---|---|---|
| 01_clean_prescription.png | clean | PRESCRIPTION | GOOD | 7/7 | 14990 ms | 1821/291 | json_schema_strict |
| 02_clean_hospital_bill.png | clean | HOSPITAL_BILL | GOOD | 7/7 | 5745 ms | 1822/214 | json_schema_strict |
| 03_phone_photo_bill.png | skew, blur, low_contrast, shadow | HOSPITAL_BILL | GOOD | 6/6 | 5969 ms | 1859/273 | json_schema_strict |
| 04_handwritten_prescription.png | handwriting, stamp | PRESCRIPTION | GOOD | 5/5 | 11519 ms | 1821/286 | json_schema_strict |
| 05_altered_pharmacy_bill.png | altered_amount | PHARMACY_BILL | GOOD | 5/5 | 6605 ms | 1824/178 | json_schema_strict |
| 06_two_page_bill.pdf | clean | HOSPITAL_BILL | GOOD | 6/6 | 17324 ms | 2895/347 | json_schema_strict |

## 01_clean_prescription.png

Quality flags: none. Unreadable fields: none. Model confidence: 1.0.

| Field | Expected | Extracted | OK |
|---|---|---|---|
| detected_document_type | PRESCRIPTION | PRESCRIPTION | ✅ |
| patient_name | Rajesh Kumar | Rajesh Kumar | ✅ |
| doctor_name | Arun Sharma | Dr. Arun Sharma | ✅ |
| registration_number | KA/45678/2015 | KA/45678/2015 | ✅ |
| date | 2024-11-01 | 2024-11-01 | ✅ |
| diagnoses | ['Viral Fever'] | ['Viral Fever'] | ✅ |
| tests_ordered | ['CBC', 'Dengue NS1'] | ['CBC', 'Dengue NS1'] | ✅ |

## 02_clean_hospital_bill.png

Quality flags: none. Unreadable fields: none. Model confidence: 0.99.

| Field | Expected | Extracted | OK |
|---|---|---|---|
| detected_document_type | HOSPITAL_BILL | HOSPITAL_BILL | ✅ |
| hospital_name | City Medical Centre | CITY MEDICAL CENTRE | ✅ |
| patient_name | Rajesh Kumar | Rajesh Kumar | ✅ |
| bill_number | CMC/2024/08321 | CMC/2024/08321 | ✅ |
| date | 2024-11-01 | 2024-11-01 | ✅ |
| total_amount | 1500 | 1500 | ✅ |
| line_item_count | 3 | 3 | ✅ |

## 03_phone_photo_bill.png

Quality flags: none. Unreadable fields: none. Model confidence: 0.95.

| Field | Expected | Extracted | OK |
|---|---|---|---|
| detected_document_type | HOSPITAL_BILL | HOSPITAL_BILL | ✅ |
| hospital_name | Apollo Hospitals | APOLLO HOSPITALS | ✅ |
| patient_name | Deepak Shah | Deepak Shah | ✅ |
| date | 2024-11-03 | 2024-11-03 | ✅ |
| total_amount | 4500 | 4500.0 | ✅ |
| line_item_count | 2 | 2 | ✅ |

## 04_handwritten_prescription.png

Quality flags: none. Unreadable fields: none. Model confidence: 0.98.

| Field | Expected | Extracted | OK |
|---|---|---|---|
| detected_document_type | PRESCRIPTION | PRESCRIPTION | ✅ |
| patient_name | Vikram Joshi | Vikram Joshi | ✅ |
| registration_number | GJ/56789/2014 | GJ/56789/2014 | ✅ |
| date | 2024-10-15 | 2024-10-15 | ✅ |
| diagnoses | ['diabetes', 'hypertension'] | ['Type 2 Diabetes Mellitus [T2DM]', 'Hypertension [HTN]'] | ✅ |

## 05_altered_pharmacy_bill.png

Quality flags: DOCUMENT_ALTERATION. Unreadable fields: none. Model confidence: 0.95.

| Field | Expected | Extracted | OK |
|---|---|---|---|
| detected_document_type | PHARMACY_BILL | PHARMACY_BILL | ✅ |
| patient_name | Rajesh Kumar | Rajesh Kumar | ✅ |
| date | 2024-11-01 | 2024-11-01 | ✅ |
| line_item_count | 2 | 2 | ✅ |
| quality_flags | ['DOCUMENT_ALTERATION'] | ['DOCUMENT_ALTERATION'] | ✅ |

## 06_two_page_bill.pdf

Quality flags: none. Unreadable fields: none. Model confidence: 0.98.

| Field | Expected | Extracted | OK |
|---|---|---|---|
| detected_document_type | HOSPITAL_BILL | HOSPITAL_BILL | ✅ |
| hospital_name | Manipal Hospitals | MANIPAL HOSPITALS | ✅ |
| patient_name | Priya Singh | Priya Singh | ✅ |
| date | 2024-10-15 | 2024-10-15 | ✅ |
| total_amount | 2600 | 2600 | ✅ |
| line_item_count | 4 | 4 | ✅ |
