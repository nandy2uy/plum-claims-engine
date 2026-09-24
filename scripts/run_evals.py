import json
import requests
from pathlib import Path
from decimal import Decimal

API_URL = "http://127.0.0.1:8000/api/v1/claims/process"
BASE_DIR = Path(__file__).resolve().parent.parent
TEST_CASES_FILE = BASE_DIR / "data" / "test_cases.json"

def map_plum_test_to_api_payload(case: dict) -> dict:
    case_input = case.get("input", {})
    
    payload = {
        "claim_id": case.get("case_id", "UNKNOWN"),
        "member_id": case_input.get("member_id", "UNKNOWN"),
        "patient_id": case_input.get("member_id", "UNKNOWN"),
        "category": case_input.get("claim_category", "UNKNOWN"),
        "treatment_date": case_input.get("treatment_date", "2024-01-01"),
        "claimed_amount": case_input.get("claimed_amount", 0),
        "hospital_name": case_input.get("hospital_name"),
        "documents": []
    }
    
    for doc in case_input.get("documents", []):
        mapped_doc = {
            "file_id": doc.get("file_name") or doc.get("file_id", "doc"),
            "file_type": doc.get("actual_type", "UNKNOWN"),
            "pre_extracted_data": {}
        }
        
        if "quality" in doc:
            mapped_doc["pre_extracted_data"]["quality"] = doc["quality"]
            
        if "patient_name_on_doc" in doc:
            dtype = doc.get("actual_type", "")
            if dtype == "PRESCRIPTION":
                mapped_doc["pre_extracted_data"]["prescription_data"] = {"patient_name": doc["patient_name_on_doc"]}
            else:
                mapped_doc["pre_extracted_data"]["bill_data"] = {"patient_name": doc["patient_name_on_doc"]}
                
        if "content" in doc:
            content = doc["content"]
            dtype = doc.get("actual_type", "")
            
            if dtype == "PRESCRIPTION":
                # Translate Plum's singular strings into our Pydantic lists
                if "diagnosis" in content and "diagnoses" not in content:
                    content["diagnoses"] = [content["diagnosis"]]
                if "treatment" in content and "tests_ordered" not in content:
                    content["tests_ordered"] = [content["treatment"]]
                mapped_doc["pre_extracted_data"]["prescription_data"] = content
            else:
                existing = mapped_doc["pre_extracted_data"].get("bill_data", {})
                mapped_doc["pre_extracted_data"]["bill_data"] = {**existing, **content}
                
        payload["documents"].append(mapped_doc)
        
    return payload

def run_evaluations():
    if not TEST_CASES_FILE.exists():
        print(f"❌ Could not find test cases at {TEST_CASES_FILE}")
        return

    with open(TEST_CASES_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
        
    test_cases = data.get("test_cases", [])
    if not test_cases:
        return

    print(f"\n🚀 IGNITING PLUM EVAL RUNNER: Processing {len(test_cases)} cases...\n" + "="*60)
    passed_count = 0
    
    for case in test_cases:
        claim_id = case.get("case_id", "UNKNOWN")
        expected = case.get("expected", {})
        
        expected_decision = "NEEDS_MEMBER_ACTION" if expected.get("decision") is None else expected.get("decision")
        
        # If Plum expects approval but omits the amount (like TC011), assume it matches claimed amount
        if "approved_amount" in expected:
            expected_amount = Decimal(str(expected["approved_amount"]))
        elif expected_decision == "APPROVED":
            expected_amount = Decimal(str(case.get("input", {}).get("claimed_amount", 0)))
        else:
            expected_amount = Decimal(0)
        
        payload = map_plum_test_to_api_payload(case)
        print(f"\n🧪 Evaluating: {claim_id} - {case.get('case_name')}")
        
        try:
            response = requests.post(API_URL, json=payload, timeout=5)
            response.raise_for_status()
            result = response.json()
        except requests.exceptions.RequestException as e:
            print(f"   ❌ API Request Failed: {e}")
            continue
            
        actual_decision = result.get("decision")
        actual_amount = Decimal(str(result.get("approved_amount", 0)))
        
        decision_match = actual_decision == expected_decision
        
        amount_match = True
        if actual_decision in ["APPROVED", "PARTIAL"]:
            amount_match = actual_amount == expected_amount
            
        if decision_match and amount_match:
            print(f"   ✅ PASS: Decision matched ({actual_decision})")
            if actual_decision in ["APPROVED", "PARTIAL"]:
                print(f"      Amount matched exactly: ₹{actual_amount}")
            print(f"      Reason: {result.get('reason')}")
            passed_count += 1
        else:
            print(f"   ❌ FAIL: Mismatch detected")
            if not decision_match:
                print(f"      Expected Decision : {expected_decision}")
                print(f"      Actual Decision   : {actual_decision}")
                print(f"      Reason Given      : {result.get('reason')}")
            if not amount_match:
                print(f"      Expected Amount   : ₹{expected_amount}")
                print(f"      Actual Amount     : ₹{actual_amount}")
                
            degraded = result.get("degraded_components", [])
            if degraded:
                print(f"      ⚠️ Degraded Agents: {degraded}")

    print("\n" + "="*60)
    print(f"📊 FINAL SCORE: {passed_count}/{len(test_cases)} Passed")
    
    if passed_count == len(test_cases):
        print("🏆 FLAWLESS VICTORY. The architecture is mathematically perfect.")

if __name__ == "__main__":
    run_evaluations()