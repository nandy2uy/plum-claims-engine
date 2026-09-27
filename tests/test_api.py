import base64

import pytest
from fastapi.testclient import TestClient

import src.main as main
from src.core.storage import claim_store
from tests.conftest import claim


@pytest.fixture
def client():
    claim_store.clear()
    with TestClient(main.app) as c:
        yield c
    claim_store.clear()


def _form(**overrides):
    data = {"member_id": "EMP001", "category": "CONSULTATION", "treatment_date": "2024-11-01",
            "claimed_amount": "1,500", "file_types": ["PRESCRIPTION", "HOSPITAL_BILL"]}
    data.update(overrides)
    return data


FILES = [("files", ("rx.jpg", b"\xff\xd8fake", "image/jpeg")), ("files", ("bill.pdf", b"%PDF-1.4", "application/pdf"))]


def test_health_reports_config_and_roster_warnings(client):
    body = client.get("/health").json()
    assert body["status"] == "ok" and body["policy_id"] == "PLUM_GHI_2024"
    assert any("DEP003" in w for w in body["policy_integrity_warnings"])


def test_json_claim_round_trip_and_lookup(client):
    r = client.post("/api/v1/claims", json=claim(claim_id="API-1"))
    assert r.status_code == 200 and r.json()["decision"] == "APPROVED" and r.json()["trace"]
    assert client.get("/api/v1/claims/API-1").json()["claim_id"] == "API-1"
    assert client.get("/api/v1/claims/NOPE").status_code == 404


def test_bad_json_input_is_readable_422(client):
    r = client.post("/api/v1/claims", json=claim(date="15-10-2024"))
    assert r.status_code == 422 and any("treatment_date" in d for d in r.json()["detail"])


def test_upload_validation_errors_are_422_not_500(client):
    assert client.post("/api/v1/claims/upload", data=_form(treatment_date="15-10-2024"), files=FILES).status_code == 422
    assert client.post("/api/v1/claims/upload", data=_form(claimed_amount="abc"), files=FILES).status_code == 422
    assert client.post("/api/v1/claims/upload", data=_form(file_types=["PRESCRIPTION"]), files=FILES).status_code == 422


def test_upload_rejects_unsupported_and_oversized_files(client, monkeypatch):
    r = client.post("/api/v1/claims/upload", data=_form(file_types=["PRESCRIPTION"]),
                    files=[("files", ("notes.txt", b"hi", "text/plain"))])
    assert r.status_code == 415
    monkeypatch.setattr(main.get_settings(), "max_upload_mb", 0.000001)
    assert client.post("/api/v1/claims/upload", data=_form(), files=FILES).status_code == 413


def test_upload_without_llm_goes_to_manual_review_and_stores_no_file_bytes(client):
    r = client.post("/api/v1/claims/upload", data=_form(), files=FILES)
    body = r.json()
    assert r.status_code == 200 and body["decision"] == "MANUAL_REVIEW" and "EXTRACTION_INCOMPLETE" in body["review_reasons"]
    stored = claim_store._claims[body["claim_id"]]
    assert not any(isinstance(v, str) and len(v) > 200 for v in stored.__dict__.values())  # no base64 retained


def test_upload_gate_stops_before_llm(client):
    r = client.post("/api/v1/claims/upload", data=_form(file_types=["PRESCRIPTION", "PRESCRIPTION"]), files=FILES)
    body = r.json()
    assert body["status"] == "NEEDS_MEMBER_ACTION" and body["decision"] is None and "hospital bill" in body["member_message"]


def test_pipeline_crash_returns_manual_review_with_trace(client, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("unexpected bug")
    monkeypatch.setattr(client.app.state.orchestrator.consistency.__class__, "evaluate", boom, raising=True)
    monkeypatch.setattr(client.app.state.orchestrator.gate, "evaluate", boom)
    r = client.post("/api/v1/claims", json=claim(claim_id="CRASH"))
    body = r.json()
    assert r.status_code == 200 and body["decision"] == "MANUAL_REVIEW" and "PIPELINE_ERROR" in body["review_reasons"]
    assert [e["component"] for e in body["trace"]][:1] == ["INTAKE"]  # work done before the crash is kept


def test_test_case_runner(client):
    cases = client.get("/api/v1/test-cases").json()
    assert len(cases) == 12
    body = client.post("/api/v1/test-cases/TC010/run").json()
    assert body["passed"] and body["decision"]["approved_amount"] == "3240.00"
    assert client.get("/api/v1/claims").json() == []  # demo runs never pollute real claim history


def test_fixture_documents_are_rejected_when_not_enabled(client, monkeypatch):
    monkeypatch.setattr(main.get_settings(), "allow_fixture_documents", False)
    r = client.post("/api/v1/claims", json=claim())
    assert r.status_code == 422 and "pre_extracted_data is not accepted" in r.json()["detail"][0]
    # the demo runner uses the server's own fixtures and still works
    assert client.post("/api/v1/test-cases/TC004/run").json()["passed"]


def test_upload_without_llm_reports_extraction_not_diagnosis(client):
    body = client.post("/api/v1/claims/upload", data=_form(), files=FILES).json()
    assert "EXTRACTION_INCOMPLETE" in body["review_reasons"] and "DIAGNOSIS_UNREADABLE" not in body["review_reasons"]


def test_runner_replays_tc011_even_with_fault_injection_off(client, monkeypatch):
    monkeypatch.setattr(main.get_settings(), "allow_fault_injection", False)
    body = client.post("/api/v1/test-cases/TC011/run").json()
    assert body["passed"] and body["decision"]["degraded_components"] == ["FRAUD"]
