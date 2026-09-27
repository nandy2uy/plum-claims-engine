"""Stage execution with timeouts, fault isolation and (gated) fault injection.

Component contract
-------------------
run_stage(ledger, component, fn, *args, timeout, critical, fallback, injector, offload, **kwargs)
    -> StageResult(ok, value, error, timed_out)
  Runs one pipeline stage. Coroutine functions are awaited under asyncio.wait_for;
  sync functions run inline, or in a worker thread when offload=True so the
  timeout is enforceable (used for stages that would do I/O in production).
  On ANY exception or timeout it never raises. It writes an ERROR event to the
  ledger (component, exception type, message, whether the stage was critical,
  whether the fault was injected, and a confidence factor), then returns
  ok=False with `fallback` as the value. The orchestrator decides what a
  failed stage means: critical stages route the claim to MANUAL_REVIEW, and
  non-critical stages degrade it.

FaultInjector(submission, settings, ledger)
  Honours ClaimSubmission.simulate_component_failure ONLY when
  settings.allow_fault_injection is on, and logs either way. v1 honoured the
  flag unconditionally, so any caller could switch off fraud detection on their
  own claim (TC009 + flag -> APPROVED 4,320).
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from dataclasses import dataclass
from typing import Any, Callable, Generic, Optional, TypeVar

from src.models.trace import Stopwatch, TraceLedger

T = TypeVar("T")
logger = logging.getLogger("claims.fault")


class InjectedFault(RuntimeError):
    """Raised on purpose to exercise graceful degradation."""


@dataclass
class StageResult(Generic[T]):
    ok: bool
    value: Optional[T]
    error: Optional[str] = None
    timed_out: bool = False


class FaultInjector:
    def __init__(self, submission, settings, ledger: TraceLedger):
        self.target: Optional[str] = None
        if not submission.simulate_component_failure:
            return
        if settings.allow_fault_injection:
            self.target = submission.simulate_failure_component
            ledger.log(
                "FAULT_INJECTION", "INFO", summary=f"Fault injection armed: {self.target} will be forced to fail.",
                evidence={"target": self.target},
            )
        else:
            ledger.log(
                "FAULT_INJECTION", "WARN",
                summary="simulate_component_failure was requested but fault injection is disabled in this "
                        "environment (ALLOW_FAULT_INJECTION=false); the request was ignored and all checks ran.",
                evidence={"requested_target": submission.simulate_failure_component},
            )

    def should_fail(self, component: str) -> bool:
        return self.target == component


async def run_stage(
    ledger: TraceLedger,
    component: str,
    fn: Callable[..., Any],
    *args,
    timeout: float,
    critical: bool,
    fallback: Any = None,
    injector: Optional[FaultInjector] = None,
    offload: bool = False,
    degraded_confidence: float = 0.70,
    **kwargs,
) -> StageResult:
    watch = Stopwatch()
    injected = bool(injector and injector.should_fail(component))
    try:
        if injected:
            raise InjectedFault(f"Simulated {component} outage (requested via simulate_component_failure).")
        if inspect.iscoroutinefunction(fn):
            value = await asyncio.wait_for(fn(*args, **kwargs), timeout=timeout)
        elif offload:
            value = await asyncio.wait_for(asyncio.to_thread(fn, *args, **kwargs), timeout=timeout)
        else:
            value = fn(*args, **kwargs)
        return StageResult(ok=True, value=value)
    except asyncio.TimeoutError:
        message = f"{component} did not finish within {timeout:.1f}s"
        _log_failure(ledger, component, "TimeoutError", message, critical, injected, watch, degraded_confidence)
        return StageResult(ok=False, value=fallback, error=message, timed_out=True)
    except Exception as exc:  # noqa: BLE001 - isolation boundary by design
        _log_failure(ledger, component, type(exc).__name__, str(exc), critical, injected, watch, degraded_confidence)
        return StageResult(ok=False, value=fallback, error=f"{type(exc).__name__}: {exc}")


def _log_failure(ledger, component, exc_type, message, critical, injected, watch, degraded_confidence):
    consequence = (
        "claim routed to manual review because this stage is required for a decision"
        if critical else "stage skipped; pipeline continued with reduced confidence and manual review recommended"
    )
    ledger.log(
        component, "ERROR", effect="ESCALATE" if critical else "DEGRADE", rule_id="stage_failure",
        summary=f"{component} failed ({exc_type}: {message}) — {consequence}.",
        evidence={"exception": exc_type, "message": message, "critical": critical, "injected": injected},
        error_details=message, duration_ms=watch.ms(),
        confidence_factor=degraded_confidence, confidence_scope="ALL",
    )
    log = logger.warning if injected else logger.error
    log("claim=%s component=%s critical=%s injected=%s error=%s: %s",
        ledger.claim_id, component, critical, injected, exc_type, message)
