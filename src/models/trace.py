import time
from contextlib import contextmanager
from pydantic import BaseModel, Field, ConfigDict
from typing import List, Optional, Dict, Any, Literal
from datetime import datetime, timezone
from decimal import Decimal


class TraceEvent(BaseModel):
    # Forbid extra fields so our agents don't hallucinate random JSON keys
    model_config = ConfigDict(extra="forbid")

    seq: int
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    component: str
    rule_id: Optional[str] = None
    outcome: Literal["PASS", "FAIL", "WARN", "SKIP", "NOT_EVALUABLE", "ERROR"]
    effect: Literal["NONE", "BLOCK", "REJECT", "ADJUST_AMOUNT", "ESCALATE"]

    policy_ref: Optional[str] = None
    interpretation_ref: Optional[str] = None
    evidence: Dict[str, Any] = Field(default_factory=dict)

    amount_before: Optional[Decimal] = None
    amount_after: Optional[Decimal] = None

    confidence_factor: Optional[float] = None
    duration_ms: int
    error_details: Optional[str] = None


class TraceLedger(BaseModel):
    """Central, append-only audit trail for a single claim's pipeline run.

    Every agent writes into the same ledger so the full decision can be
    reconstructed from one artifact instead of stitching together per-agent logs.
    """

    events: List[TraceEvent] = Field(default_factory=list)

    def append(self, event: TraceEvent):
        self.events.append(event)

    def log(
        self,
        component: str,
        outcome: str,
        effect: str = "NONE",
        duration_ms: int = 0,
        **kwargs,
    ) -> TraceEvent:
        """Convenience wrapper so agents don't hand-roll `seq=len(events)+1` everywhere."""
        event = TraceEvent(
            seq=len(self.events) + 1,
            component=component,
            outcome=outcome,
            effect=effect,
            duration_ms=duration_ms,
            **kwargs,
        )
        self.append(event)
        return event

    @contextmanager
    def timed(self, component: str, outcome: str = "PASS", effect: str = "NONE", **kwargs):
        """Context manager that times a block and logs it on exit.

        Usage:
            with ledger.timed("DOC_GATE", outcome="FAIL", effect="BLOCK", evidence={...}):
                ...
        Callers can mutate the returned holder's `outcome`/`effect`/`evidence`
        before the block ends if the result isn't known up front.
        """
        start = time.time()
        holder = _TimedEventHolder(outcome=outcome, effect=effect, kwargs=kwargs)
        try:
            yield holder
        finally:
            self.log(
                component=component,
                outcome=holder.outcome,
                effect=holder.effect,
                duration_ms=int((time.time() - start) * 1000),
                **holder.kwargs,
            )

    def get_degraded_components(self) -> List[str]:
        # Scans the ledger to find any component that threw an error
        return list(dict.fromkeys(e.component for e in self.events if e.outcome == "ERROR"))

    def get_warnings(self) -> List[TraceEvent]:
        return [e for e in self.events if e.outcome == "WARN"]


class _TimedEventHolder:
    def __init__(self, outcome: str, effect: str, kwargs: dict):
        self.outcome = outcome
        self.effect = effect
        self.kwargs = kwargs
