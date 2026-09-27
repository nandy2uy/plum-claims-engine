"""Shared fixtures. Every test builds its own orchestrator + store: no global state leaks between tests."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
# Never hit the network from tests, even if a real key is in .env (env vars override .env).
os.environ["GEMINI_API_KEY"] = ""
os.environ["OPENAI_API_KEY"] = ""
os.environ["ALLOW_FIXTURE_DOCUMENTS"] = "true"  # the suite drives the pipeline with fixture extractions

from src.core.config import Settings, get_interpretation, get_policy, load_test_cases  # noqa: E402
from src.core.orchestrator import ClaimsOrchestrator  # noqa: E402
from src.core.storage import ClaimStore  # noqa: E402
from src.models.claim import ClaimSubmission  # noqa: E402
from src.models.trace import TraceLedger  # noqa: E402


@pytest.fixture
def policy():
    return get_policy()


@pytest.fixture
def interp():
    return get_interpretation()


@pytest.fixture
def settings():
    return Settings(gemini_api_key="", openai_api_key="", allow_fault_injection=True, allow_fixture_documents=True)


@pytest.fixture
def store():
    return ClaimStore()


@pytest.fixture
def ledger():
    return TraceLedger("TEST")


@pytest.fixture
def cases():
    return {c["case_id"]: c for c in load_test_cases()}


@pytest.fixture
def run(settings, store):
    """run(payload_dict, backend=None, settings=None) -> ClaimDecision, through the full pipeline."""
    async def _run(payload: dict, backend=None, settings_override=None):
        orch = ClaimsOrchestrator(settings=settings_override or settings, store=store, backend=backend)
        return await orch.process(ClaimSubmission.model_validate(payload))
    return _run


# ---------------------------------------------------------------- payload builders

def rx(file_id="RX", **data):
    return {"file_id": file_id, "file_name": f"{file_id.lower()}.jpg", "file_type": "PRESCRIPTION",
            "pre_extracted_data": {"prescription_data": data}}


def bill(file_id="BILL", file_type="HOSPITAL_BILL", **data):
    return {"file_id": file_id, "file_name": f"{file_id.lower()}.jpg", "file_type": file_type,
            "pre_extracted_data": {"bill_data": data}}


def report(file_id="LAB", **data):
    return {"file_id": file_id, "file_name": f"{file_id.lower()}.jpg", "file_type": "LAB_REPORT",
            "pre_extracted_data": {"report_data": data}}


def line(description, amount, **extra):
    return {"description": description, "amount": amount, **extra}


def claim(member="EMP002", category="CONSULTATION", date="2024-11-01", amount=1000, documents=None, **extra):
    if documents is None:
        documents = [rx(diagnoses=["Viral Fever"], registration_number="KA/45678/2015"),
                     bill(line_items=[line("Consultation Fee", amount)])]
    return {"member_id": member, "category": category, "treatment_date": date, "claimed_amount": amount,
            "documents": documents, **extra}


def events(decision, component=None, rule_id=None, outcome=None):
    return [e for e in decision.trace
            if (component is None or e.component == component) and (rule_id is None or e.rule_id == rule_id)
            and (outcome is None or e.outcome == outcome)]
