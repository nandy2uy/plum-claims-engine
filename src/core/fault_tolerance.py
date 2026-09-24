import time
from functools import wraps
from src.models.trace import TraceLedger, TraceEvent

def isolate_fault(component_name: str, fallback_return):
    """
    Wraps an agent's method. If the agent throws a Python exception, 
    this catches it, writes the error to the trace ledger, and returns a safe fallback.
    """
    def decorator(func):
        # We handle async functions smoothly since our extractor is async
        if getattr(func, '_is_coroutine', False) or (hasattr(func, '__code__') and func.__code__.co_flags & 0x80):
            @wraps(func)
            async def async_wrapper(*args, **kwargs):
                ledger = kwargs.get('ledger') or next((arg for arg in args if isinstance(arg, TraceLedger)), None)
                start_time = time.time()
                try:
                    return await func(*args, **kwargs)
                except Exception as e:
                    if ledger:
                        ledger.append(TraceEvent(
                            seq=len(ledger.events) + 1, component=component_name, outcome="ERROR",
                            effect="ESCALATE", evidence={"exception": type(e).__name__, "message": str(e)},
                            duration_ms=int((time.time() - start_time) * 1000)
                        ))
                    print(f"⚠️ FAULT ISOLATED IN {component_name}: {str(e)}")
                    return fallback_return
            return async_wrapper
        else:
            @wraps(func)
            def sync_wrapper(*args, **kwargs):
                ledger = kwargs.get('ledger') or next((arg for arg in args if isinstance(arg, TraceLedger)), None)
                start_time = time.time()
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    if ledger:
                        ledger.append(TraceEvent(
                            seq=len(ledger.events) + 1, component=component_name, outcome="ERROR",
                            effect="ESCALATE", evidence={"exception": type(e).__name__, "message": str(e)},
                            duration_ms=int((time.time() - start_time) * 1000)
                        ))
                    print(f"⚠️ FAULT ISOLATED IN {component_name}: {str(e)}")
                    return fallback_return
            return sync_wrapper
    return decorator