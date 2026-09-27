"""The decision trace: one append-only ledger per claim, returned verbatim in the API.

Design goals (assignment: "reconstruct exactly why any claim got any decision
just from the trace"):
- Every rule that is evaluated writes an event, whether it PASSes, FAILs, is
  SKIPped (and why), or is NOT_EVALUABLE (and why). v1 only logged failures and
  money adjustments, so a clean approval's trace could not show that waiting
  periods or exclusions had been checked at all.
- Every event has a plain-English `summary`, so an ops reviewer does not have to
  decode raw evidence JSON.
- Confidence is explainable: any event that lowers confidence carries
  `confidence_factor` (+ scope), and the final score is base x product(factors).
- Concurrency-safe: agents that run concurrently write to forked child ledgers
  which are merged back in a fixed order, so traces are deterministic even
  though stages run in parallel.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

Outcome = Literal["PASS", "FAIL", "WARN", "SKIP", "NOT_EVALUABLE", "ERROR", "INFO"]
Effect = Literal["NONE", "BLOCK", "REJECT", "REVIEW", "ADJUST_AMOUNT", "ESCALATE", "DEGRADE"]
ConfidenceScope = Literal["ALL", "PAYOUT"]


class TraceEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    seq: int
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    component: str
    rule_id: Optional[str] = None
    outcome: Outcome
    effect: Effect = "NONE"
    summary: str = ""
    policy_ref: Optional[str] = None
    interpretation_ref: Optional[str] = None
    evidence: Dict[str, Any] = Field(default_factory=dict)
    amount_before: Optional[Decimal] = None
    amount_after: Optional[Decimal] = None
    # Multiplier applied to the claim's confidence. PAYOUT-scoped factors only
    # apply when money is paid (APPROVED/PARTIAL): e.g. an unverified patient
    # name matters for a payout, not for a rejection on a permanent exclusion.
    confidence_factor: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    confidence_scope: ConfidenceScope = "ALL"
    duration_ms: int = 0
    error_details: Optional[str] = None


class TraceLedger:
    def __init__(self, claim_id: str = ""):
        self.claim_id = claim_id
        self._events: List[TraceEvent] = []
        self._lock = threading.Lock()

    @property
    def events(self) -> List[TraceEvent]:
        with self._lock:
            return list(self._events)

    def log(self, component: str, outcome: Outcome, effect: Effect = "NONE", summary: str = "", **kwargs) -> TraceEvent:
        with self._lock:
            event = TraceEvent(
                seq=len(self._events) + 1, component=component, outcome=outcome,
                effect=effect, summary=summary, **kwargs,
            )
            self._events.append(event)
            return event

    def fork(self) -> "TraceLedger":
        return TraceLedger(self.claim_id)

    def merge(self, child: "TraceLedger") -> None:
        for event in child.events:
            with self._lock:
                self._events.append(event.model_copy(update={"seq": len(self._events) + 1}))

    def degraded_components(self) -> List[str]:
        return list(dict.fromkeys(e.component for e in self.events if e.outcome == "ERROR"))

    def confidence_events(self) -> List[TraceEvent]:
        return [e for e in self.events if e.confidence_factor is not None]


class Stopwatch:
    def __init__(self) -> None:
        self._start = time.perf_counter()

    def ms(self) -> int:
        return int((time.perf_counter() - self._start) * 1000)
