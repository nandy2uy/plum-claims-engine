"""Strict, per-case checks for the 12 test cases (expected outcome AND system_must).

Component contract
-------------------
check_case(case, result: dict, baseline: Optional[dict] = None) -> List[Check]
    `result` is a ClaimDecision serialised with model_dump(mode="json").
    `baseline` (TC011 only) is the same claim run WITHOUT the simulated failure,
    so "confidence lower than a normal full-pipeline approval" is tested against
    a real run, not a guessed constant.
Check(name, passed, detail). A case passes only if every check passes.
Errors: none.

v1's eval passed a case if the decision matched and a reason code appeared
anywhere in the free-text reason; confidence thresholds and system_must bullets
were not checked at all.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Callable, Dict, List, Optional


@dataclass
class Check:
    name: str
    passed: bool
    detail: str = ""


def _text(result: dict) -> str:
    return " ".join([result.get("member_message") or "", result.get("reason") or ""])


def _contains(result: dict, *needles: str) -> Check:
    text = _text(result).lower()
    missing = [n for n in needles if n.lower() not in text]
    return Check(f"message mentions {', '.join(needles)}", not missing, f"missing: {missing}" if missing else "")


def _action(result: dict, action: str, file_id: Optional[str] = None) -> Check:
    ok = any(a.get("action") == action and (file_id is None or a.get("file_id") == file_id)
             for a in result.get("required_actions", []))
    return Check(f"required_actions has {action}" + (f" for {file_id}" if file_id else ""), ok,
                 str(result.get("required_actions")))


def _tc001(r, b):
    return [_contains(r, "prescription", "hospital bill", "dr_sharma_prescription.jpg", "another_prescription.jpg"),
            _action(r, "UPLOAD_DOCUMENT")]


def _tc002(r, b):
    return [_contains(r, "blurry_bill.jpg", "pharmacy bill", "re-upload"), _action(r, "REUPLOAD_DOCUMENT", "F004"),
            Check("not rejected", r.get("decision") != "REJECTED")]


def _tc003(r, b):
    return [_contains(r, "Rajesh Kumar", "Arjun Mehta")]


def _tc004(r, b):
    fb = r.get("financial_breakdown") or {}
    return [Check("co-pay of 150 shown", Decimal(str(fb.get("copay_amount", 0))) == Decimal(150), str(fb.get("copay_amount"))),
            _contains(r, "₹150")]


def _tc005(r, b):
    return [_contains(r, "30 Nov 2024", "diabetes")]


def _tc006(r, b):
    items = {i["description"]: i for i in r.get("line_item_breakdown", [])}
    rct, whitening = items.get("Root Canal Treatment", {}), items.get("Teeth Whitening", {})
    return [Check("root canal APPROVED", rct.get("status") == "APPROVED", str(rct)),
            Check("whitening REJECTED with a line-level reason", whitening.get("status") == "REJECTED"
                  and bool(whitening.get("reason")), str(whitening))]


def _tc007(r, b):
    return [_contains(r, "pre-authorisation", "resubmit"), _action(r, "OBTAIN_PRE_AUTH")]


def _tc008(r, b):
    return [_contains(r, "₹5,000", "₹7,500")]


def _tc009(r, b):
    signals = " ".join(r.get("fraud_signals", []))
    return [Check("SAME_DAY_LIMIT_EXCEEDED signal present", "SAME_DAY_LIMIT_EXCEEDED" in signals, signals),
            Check("signals name the earlier claims", all(c in signals for c in ("CLM_0081", "CLM_0082", "CLM_0083")), signals),
            Check("not auto-rejected", r.get("decision") != "REJECTED")]


def _tc010(r, b):
    fb = r.get("financial_breakdown") or {}
    steps = " ".join(fb.get("steps", []))
    order_ok = steps.find("Network discount") != -1 and steps.find("Network discount") < steps.find("Co-pay")
    return [Check("network discount 900", Decimal(str(fb.get("network_discount_amount", 0))) == Decimal(900), str(fb)),
            Check("co-pay 360", Decimal(str(fb.get("copay_amount", 0))) == Decimal(360), str(fb.get("copay_amount"))),
            Check("discount applied before co-pay (breakdown order)", order_ok, steps)]


def _tc011(r, b):
    checks = [Check("failed component visible", bool(r.get("degraded_components")), str(r.get("degraded_components"))),
              Check("manual review recommended", bool(r.get("manual_review_recommended"))),
              _contains(r, "manual review")]
    if b is not None:
        checks.append(Check("confidence below the full-pipeline baseline",
                            r.get("confidence_score", 1) < b.get("confidence_score", 0),
                            f"{r.get('confidence_score')} vs baseline {b.get('confidence_score')}"))
    return checks


def _tc012(r, b):
    return [_contains(r, "obesity")]


SYSTEM_MUST: Dict[str, Callable] = {
    "TC001": _tc001, "TC002": _tc002, "TC003": _tc003, "TC004": _tc004, "TC005": _tc005, "TC006": _tc006,
    "TC007": _tc007, "TC008": _tc008, "TC009": _tc009, "TC010": _tc010, "TC011": _tc011, "TC012": _tc012,
}


def check_case(case: dict, result: dict, baseline: Optional[dict] = None) -> List[Check]:
    expected = case.get("expected", {})
    checks: List[Check] = []
    if expected.get("decision") is None:
        checks.append(Check("stopped before a decision (status NEEDS_MEMBER_ACTION, decision null)",
                            result.get("status") == "NEEDS_MEMBER_ACTION" and result.get("decision") is None,
                            f"status={result.get('status')} decision={result.get('decision')}"))
    else:
        checks.append(Check(f"decision {expected['decision']}", result.get("decision") == expected["decision"],
                            f"got {result.get('decision')}"))
    if "approved_amount" in expected:
        got = Decimal(str(result.get("approved_amount", 0)))
        checks.append(Check(f"approved_amount {expected['approved_amount']}", got == Decimal(str(expected["approved_amount"])),
                            f"got {got}"))
    if "confidence_score" in expected:
        m = re.search(r"above\s+([\d.]+)", str(expected["confidence_score"]))
        if m:
            floor = float(m.group(1))
            checks.append(Check(f"confidence > {floor}", result.get("confidence_score", 0) > floor,
                                f"got {result.get('confidence_score')}"))
    for code in expected.get("rejection_reasons", []):
        checks.append(Check(f"rejection_reasons contains {code}", code in result.get("rejection_reasons", []),
                            f"got {result.get('rejection_reasons')}"))
    extra = SYSTEM_MUST.get(case["case_id"])
    if extra:
        checks += extra(result, baseline)
    return checks
