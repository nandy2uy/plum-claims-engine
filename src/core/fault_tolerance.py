import time
import inspect
from functools import wraps
from src.models.trace import TraceLedger


def _find_ledger(args, kwargs):
    if "ledger" in kwargs and isinstance(kwargs["ledger"], TraceLedger):
        return kwargs["ledger"]
    return next((arg for arg in args if isinstance(arg, TraceLedger)), None)


def isolate_fault(component_name: str, fallback_return):
    """Wraps an agent's method. If it throws, this catches it, writes an ERROR
    TraceEvent to the ledger (if one was passed to the wrapped call), and
    returns a safe fallback instead of propagating.

    This is what makes graceful degradation possible: a failing non-critical
    agent never takes down the request, it just shows up in
    `ledger.get_degraded_components()` and the orchestrator adjusts confidence
    and routing accordingly.
    """

    def decorator(func):
        if inspect.iscoroutinefunction(func):
            @wraps(func)
            async def async_wrapper(*args, **kwargs):
                ledger = _find_ledger(args, kwargs)
                start_time = time.time()
                try:
                    return await func(*args, **kwargs)
                except Exception as e:
                    _log_fault(ledger, component_name, e, start_time)
                    return fallback_return
            return async_wrapper
        else:
            @wraps(func)
            def sync_wrapper(*args, **kwargs):
                ledger = _find_ledger(args, kwargs)
                start_time = time.time()
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    _log_fault(ledger, component_name, e, start_time)
                    return fallback_return
            return sync_wrapper
    return decorator


def _log_fault(ledger, component_name: str, e: Exception, start_time: float):
    if ledger:
        ledger.log(
            component=component_name,
            outcome="ERROR",
            effect="ESCALATE",
            evidence={"exception": type(e).__name__, "message": str(e)},
            duration_ms=int((time.time() - start_time) * 1000),
            error_details=str(e),
        )
    print(f"FAULT ISOLATED IN {component_name}: {str(e)}")
