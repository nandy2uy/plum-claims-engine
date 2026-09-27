"""Claim store: decided claims, for decision review, fraud history and YTD usage.

Component contract
-------------------
record(submission, decision, fingerprints)   store a decided claim (idempotent per claim_id)
get(claim_id) -> Optional[ClaimDecision]
list_recent(limit) -> List[ClaimDecision]     newest first
prior_claims(member_id, exclude_claim_id)     DECIDED claims for a member (fraud frequency).
                                              NEEDS_MEMBER_ACTION attempts don't count: the
                                              member resubmits the same claim once fixed.
paid_between(patient_ids, start, end, exclude_claim_id) -> Decimal
                                              approved payouts in a window (annual limits)
find_by_fingerprints(fps, exclude_claim_id)   earlier claims sharing a document hash or
                                              bill number (duplicate detection)
clear()
Errors: none.

Trade-offs (documented): process-local, in-memory, bounded (oldest records are
evicted beyond `max_records`). Raw document bytes are NEVER retained — v1 kept
every uploaded image as base64 inside the stored submission, an unbounded memory
leak and needless retention of medical data. At 10x load this interface is
backed by Postgres (see ARCHITECTURE.md); callers do not change.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Iterable, List, Optional, Tuple

from src.models.claim import ClaimDecision, ClaimSubmission


@dataclass(frozen=True)
class StoredClaim:
    claim_id: str
    member_id: str
    patient_id: str
    treatment_date: date
    claimed_amount: Decimal
    approved_amount: Decimal
    status: str
    decision: Optional[str]
    provider: Optional[str]
    fingerprints: Tuple[str, ...]


class ClaimStore:
    def __init__(self, max_records: int = 5000):
        self._lock = threading.Lock()
        self._max = max_records
        self._claims: "OrderedDict[str, StoredClaim]" = OrderedDict()
        self._decisions: "OrderedDict[str, ClaimDecision]" = OrderedDict()

    def record(self, submission: ClaimSubmission, decision: ClaimDecision, fingerprints: Iterable[str] = ()) -> None:
        stored = StoredClaim(
            claim_id=decision.claim_id,
            member_id=submission.member_id,
            patient_id=submission.patient_id or submission.member_id,
            treatment_date=submission.treatment_date,
            claimed_amount=submission.claimed_amount,
            approved_amount=decision.approved_amount,
            status=decision.status,
            decision=decision.decision,
            provider=submission.hospital_name,
            fingerprints=tuple(sorted(set(fingerprints))),
        )
        with self._lock:
            self._claims.pop(decision.claim_id, None)
            self._decisions.pop(decision.claim_id, None)
            self._claims[decision.claim_id] = stored
            self._decisions[decision.claim_id] = decision
            while len(self._claims) > self._max:
                oldest, _ = self._claims.popitem(last=False)
                self._decisions.pop(oldest, None)

    def get(self, claim_id: str) -> Optional[ClaimDecision]:
        with self._lock:
            return self._decisions.get(claim_id)

    def list_recent(self, limit: int = 50) -> List[ClaimDecision]:
        with self._lock:
            return list(reversed(self._decisions.values()))[: max(0, limit)]

    def prior_claims(self, member_id: str, exclude_claim_id: str = "") -> List[StoredClaim]:
        with self._lock:
            return [
                c for c in self._claims.values()
                if c.member_id == member_id and c.claim_id != exclude_claim_id and c.status == "DECIDED"
            ]

    def paid_between(self, patient_ids: Iterable[str], start: date, end: date, exclude_claim_id: str = "") -> Decimal:
        ids = set(patient_ids)
        with self._lock:
            return sum(
                (c.approved_amount for c in self._claims.values()
                 if c.patient_id in ids and c.claim_id != exclude_claim_id
                 and c.decision in ("APPROVED", "PARTIAL") and start <= c.treatment_date <= end),
                Decimal(0),
            )

    def find_by_fingerprints(self, fingerprints: Iterable[str], exclude_claim_id: str = "") -> List[StoredClaim]:
        fps = set(fingerprints)
        if not fps:
            return []
        with self._lock:
            return [
                c for c in self._claims.values()
                if c.claim_id != exclude_claim_id and c.status == "DECIDED" and fps.intersection(c.fingerprints)
            ]

    def clear(self) -> None:
        with self._lock:
            self._claims.clear()
            self._decisions.clear()


def _default_store() -> ClaimStore:
    from src.core.config import get_settings  # local import keeps this module dependency-light
    return ClaimStore(max_records=get_settings().store_max_records)


# Process-wide default instance used by the API; tests construct their own.
claim_store = _default_store()
