# Live extraction results

Generated 2026-09-27 07:12 UTC with `gemini` / `gemini-3.8-flash` by `python scripts/run_live_samples.py`. Documents are the mock files in `samples/` (rendered by `scripts/make_samples.py` following sample_documents_guide.md).

**Field accuracy: 26/26 expected fields extracted correctly.**

| Document | Variations | Detected | Readability | Fields correct | Latency | Tokens in/out | Schema mode |
|---|---|---|---|---|---|---|---|
| 01_clean_prescription.png | clean | PRESCRIPTION | GOOD | 7/7 | 14990 ms | 1821/291 | json_schema_strict |
| 02_clean_hospital_bill.png | clean | HOSPITAL_BILL | GOOD | 7/7 | 5745 ms | 1822/214 | json_schema_strict |
| 03_phone_photo_bill.png | skew, blur, low_contrast, shadow | HOSPITAL_BILL | GOOD | 6/6 | 5969 ms | 1859/273 | json_schema_strict |
| 04_handwritten_prescription.png | handwriting, stamp | error | — | — | — | — | SYSTEM: Vision model unavailable after 3 attempts: Error code: 503 - [{'error': {'code': 503, 'message': 'This model is currently experiencing high demand. Spikes in demand are usually temporary. Please try again later.', 'status': 'UNAVAILABLE'}}] |
| 05_altered_pharmacy_bill.png | altered_amount | error | — | — | — | — | SYSTEM: Vision model unavailable after 3 attempts: Error code: 429 - [{'error': {'code': 429, 'message': 'You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. \n* Quota exceeded for metric: generativelanguage.googleapis.com/generate_content_free_tier_requests, limit: 5, model: gemini-3.8-flash\nPlease retry in 15.183314683s.', 'status': 'RESOURCE_EXHAUSTED', 'details': [{'@type': 'type.googleapis.com/google.rpc.Help', 'links': [{'description': 'Learn more about Gemini API quotas', 'url': 'https://ai.google.dev/gemini-api/docs/rate-limits'}]}, {'@type': 'type.googleapis.com/google.rpc.QuotaFailure', 'violations': [{'quotaMetric': 'generativelanguage.googleapis.com/generate_content_free_tier_requests', 'quotaId': 'GenerateRequestsPerMinutePerProjectPerModel-FreeTier', 'quotaDimensions': {'location': 'global', 'model': 'gemini-3.8-flash'}, 'quotaValue': '5'}]}, {'@type': 'type.googleapis.com/google.rpc.RetryInfo', 'retryDelay': '15s'}]}}] |
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
