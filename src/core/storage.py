"""In-memory claim store.

Component contract
-------------------
Input:  `record(submission, decision)` after a claim has been decided.
Output: `get(claim_id)` -> stored record or None.
        `list_recent(limit)` -> most recent decisions, newest first.
        `claims_for_member_on(member_id, date_str)` -> prior claims for the
          same member on the same calendar day (used by FraudAgent when the
          request doesn't supply an explicit claims_history override).
        `claims_for_member_in_month(member_id, year, month)` -> same, monthly.
Errors: none — this is a best-effort process-local cache, not a database.

Trade-off (documented): this is intentionally a plain dict behind a lock, not
Postgres/Redis. It resets on restart and does not scale across multiple
worker processes. That's an explicit, acceptable trade-off for a take-home —
see the architecture doc's "at 10x load" section for what replaces this.
"""

import threading
from datetime import datetime
from typing import Dict, List, Optional

from src.models.claim import ClaimDecision, ClaimSubmission


class ClaimStore:
    def __init__(self):
        self._lock = threading.Lock()
        self._records: Dict[str, dict] = {}
        self._order: List[str] = []

    def record(self, submission: ClaimSubmission, decision: ClaimDecision) -> None:
        with self._lock:
            self._records[decision.claim_id] = {
                "submission": submission,
                "decision": decision,
            }
            if decision.claim_id in self._order:
                self._order.remove(decision.claim_id)
            self._order.append(decision.claim_id)

    def get(self, claim_id: str) -> Optional[dict]:
        with self._lock:
            return self._records.get(claim_id)

    def list_recent(self, limit: int = 50) -> List[dict]:
        with self._lock:
            ids = list(reversed(self._order[-limit:]))
            return [self._records[i] for i in ids]

    def claims_for_member_on(self, member_id: str, date_str: str, exclude_claim_id: str = "") -> List[dict]:
        with self._lock:
            return [
                r for r in self._records.values()
                if r["submission"].member_id == member_id
                and r["submission"].treatment_date == date_str
                and r["decision"].claim_id != exclude_claim_id
                and self._counts_toward_frequency(r)
            ]

    def claims_for_member_in_month(self, member_id: str, year: int, month: int, exclude_claim_id: str = "") -> List[dict]:
        with self._lock:
            out = []
            for r in self._records.values():
                if r["submission"].member_id != member_id or r["decision"].claim_id == exclude_claim_id:
                    continue
                if not self._counts_toward_frequency(r):
                    continue
                try:
                    d = datetime.strptime(r["submission"].treatment_date, "%Y-%m-%d")
                except ValueError:
                    continue
                if d.year == year and d.month == month:
                    out.append(r)
            return out

    @staticmethod
    def _counts_toward_frequency(record: dict) -> bool:
        # A claim stopped at the document gate (NEEDS_MEMBER_ACTION) was never
        # actually accepted for adjudication -- the member is expected to
        # resubmit the same claim with fixed documents, which will be the one
        # attempt that counts. Counting the rejected upload itself would
        # double-count a single real claim and falsely trigger same-day/
        # monthly frequency signals.
        return record["decision"].decision != "NEEDS_MEMBER_ACTION"


# Process-wide singleton — every request in this worker shares one store.
claim_store = ClaimStore()
