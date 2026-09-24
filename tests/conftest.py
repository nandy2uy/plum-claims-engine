import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from src.core.config import get_policy_config, get_interpretation_config
from src.core.storage import claim_store
from src.models.trace import TraceLedger


@pytest.fixture
def policy_config():
    return get_policy_config()


@pytest.fixture
def interpretation_config():
    return get_interpretation_config()


@pytest.fixture
def ledger():
    return TraceLedger()


@pytest.fixture(autouse=True)
def clean_claim_store():
    """The claim store is a process-wide singleton (see src/core/storage.py) so
    that fraud frequency checks can see real prior submissions. That same
    property makes tests leak state into each other unless reset — this is
    exactly the class of bug that made TC004 flaky against TC001/TC003 during
    development (three unrelated fixtures happened to share member_id +
    treatment_date)."""
    claim_store._records.clear()
    claim_store._order.clear()
    yield
    claim_store._records.clear()
    claim_store._order.clear()
