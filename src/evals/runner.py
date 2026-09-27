"""Runs the 12 test cases and renders results.

Component contract
-------------------
async run_all(settings=None, http_url=None) -> List[CaseResult]
    In-process by default (real orchestrator, fresh store per case, fault injection enabled
    for this run only). With http_url, posts each case to a running API instead.
render_markdown(results) -> str      EVAL_REPORT.md body: summary table, then per case the
                                     decision, every check, and the FULL trace.
Errors: network errors propagate in HTTP mode; nothing else raises.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional

from src.core.config import ENGINE_VERSION, Settings, config_fingerprint, get_settings, load_test_cases
from src.core.orchestrator import ClaimsOrchestrator
from src.core.storage import ClaimStore
from src.evals.adapter import case_to_payload, case_to_submission
from src.evals.checks import Check, check_case


@dataclass
class CaseResult:
    case: dict
    result: dict
    checks: List[Check]
    baseline: Optional[dict] = None
    passed: bool = field(init=False)

    def __post_init__(self):
        self.passed = all(c.passed for c in self.checks)


async def _decide(case, settings, http_url, simulate_failure=None) -> dict:
    if http_url:
        import httpx
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(http_url.rstrip("/") + "/api/v1/claims", json=case_to_payload(case, simulate_failure))
            r.raise_for_status()
            return r.json()
    decision = await ClaimsOrchestrator(settings=settings, store=ClaimStore()).process(case_to_submission(case, simulate_failure))
    return decision.model_dump(mode="json")


async def run_all(settings: Optional[Settings] = None, http_url: Optional[str] = None) -> List[CaseResult]:
    settings = settings or get_settings().model_copy(update={"allow_fault_injection": True, "allow_fixture_documents": True})
    results = []
    for case in load_test_cases():
        result = await _decide(case, settings, http_url)
        baseline = await _decide(case, settings, http_url, False) if case["input"].get("simulate_component_failure") else None
        results.append(CaseResult(case, result, check_case(case, result, baseline), baseline))
    return results


def _money(v) -> str:
    return "—" if v in (None, "") else f"₹{float(v):,.2f}".replace(".00", "")


def render_markdown(results: List[CaseResult]) -> str:
    passed = sum(r.passed for r in results)
    out = [
        "# Eval Report — 12 assignment test cases",
        "",
        f"Generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC} by `python scripts/generate_eval_report.py` · "
        f"engine {ENGINE_VERSION} · policy fingerprint `{config_fingerprint(get_settings().policy_file)}`",
        "",
        f"**Result: {passed}/{len(results)} cases pass every check.**",
        "",
        "A case passes only if the decision (or the early stop for TC001–TC003), the approved amount, the confidence "
        "threshold, the structured `rejection_reasons`, and a concrete check for each `system_must` bullet all hold. "
        "TC011's confidence is compared with a real run of the same claim without the injected failure. The same checks "
        "run in CI as `tests/test_eval_cases.py`.",
        "",
        "Documents are supplied as fixture extractions (`pre_extracted_data`), because the test cases describe document "
        "content rather than ship images. Every other stage is the production code path.",
        "",
        "## Interpretation calls",
        "",
        "Where the policy is ambiguous, the reading used is data in `data/interpretation.json` and is cited by the trace "
        "events that rely on it. The calls that decide test cases:",
        "",
        "- **Per-claim ceiling (TC006, TC008, TC010).** Ceiling = max(`coverage.per_claim_limit`, category `sub_limit`), "
        "compared with the *eligible* amount. TC008: consultation ₹7,500 > ₹5,000 → REJECTED. TC006: dental ₹12,000 billed, "
        "₹8,000 eligible ≤ ₹10,000 → PARTIAL. TC010: ₹4,500 ≤ ₹5,000. Treating the ₹2,000 consultation sub-limit as a hard "
        "cap would contradict TC010's ₹3,240, so sub-limits below the global limit are advisory warnings "
        "(`per_claim_limit`, `assumptions.sub_limit`).",
        "- **Pre-authorisation before limits (TC007).** Pre-auth is evaluated per bill line first; the rejected MRI line "
        "leaves nothing eligible, so the reason is PRE_AUTH_MISSING rather than PER_CLAIM_EXCEEDED (`assumptions.per_item_pre_auth`).",
        "- **Exclusion supersedes waiting period (TC012).** Obesity is both excluded and has a 365-day wait; the waiting "
        "period is logged SKIP, because telling the member they become eligible later would be false "
        "(`assumptions.exclusion_precedence`).",
        "- **Which component fails (TC011).** The case does not name one; the default is FRAUD (non-critical), so the claim "
        "is still decided, the failure is visible, and confidence drops (`simulate_failure_component`).",
        "- **Identity with no names on documents (TC007, TC009, TC011, TC012).** Not blocking; a PAYOUT-scoped confidence "
        "factor, so it lowers confidence on payouts but not on rejections (`confidence.identity_unverified`).",
        "",
        "| Case | Name | Expected | Got | Approved | Confidence | Result |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in results:
        exp = r.case["expected"]
        got = r.result.get("decision") or f"stopped ({r.result.get('status')})"
        out.append(f"| {r.case['case_id']} | {r.case['case_name']} | {exp.get('decision') or 'stop (null)'} "
                   f"{('/ ' + _money(exp['approved_amount'])) if 'approved_amount' in exp else ''} | {got} | "
                   f"{_money(r.result.get('approved_amount'))} | {r.result.get('confidence_score')} | "
                   f"{'✅ PASS' if r.passed else '❌ FAIL'} |")
    for r in results:
        res = r.result
        out += ["", "---", "", f"## {r.case['case_id']} — {r.case['case_name']}", "", f"_{r.case['description']}_", "",
                f"**Expected:** `{json.dumps(r.case['expected'], ensure_ascii=False)}`", "",
                f"**Decision:** {res.get('decision') or 'none (stopped for member action)'} · status {res.get('status')} · "
                f"approved {_money(res.get('approved_amount'))}"
                + (f" · provisional {_money(res.get('provisional_amount'))}" if res.get("provisional_amount") else "")
                + f" · confidence {res.get('confidence_score')}", ""]
        if res.get("rejection_reasons"):
            out.append(f"**Rejection reasons:** {', '.join(res['rejection_reasons'])}  ")
        if res.get("review_reasons"):
            out.append(f"**Review reasons:** {', '.join(res['review_reasons'])}  ")
        if res.get("degraded_components"):
            out.append(f"**Degraded components:** {', '.join(res['degraded_components'])}  ")
        if res.get("fraud_signals"):
            out.append("**Fraud signals:** " + "; ".join(res["fraud_signals"]) + "  ")
        out += ["", f"**Member message:** {res.get('member_message')}", "", f"**Ops reason:** {res.get('reason')}", ""]
        if res.get("line_item_breakdown"):
            out += ["| Line item | Amount | Status | Reason |", "|---|---|---|---|"]
            out += [f"| {li['description']} | {_money(li['amount'])} | {li['status']} | {li.get('reason') or ''} |"
                    for li in res["line_item_breakdown"]]
            out.append("")
        fb = res.get("financial_breakdown")
        if fb:
            out += ["**Financial breakdown:** " + " → ".join(fb.get("steps", [])), ""]
        out += ["**Confidence breakdown:** " + " × ".join(
            f"{f['factor']} ({f['component']}{'/' + f['rule_id'] if f.get('rule_id') else ''})"
            for f in res.get("confidence_breakdown", [])) + f" = {res.get('confidence_score')}", ""]
        if r.baseline:
            out += [f"Baseline without the injected failure: {r.baseline.get('decision')} at confidence "
                    f"{r.baseline.get('confidence_score')}.", ""]
        out += ["**Checks:**", ""] + [f"- {'✅' if c.passed else '❌'} {c.name}" + (f" — {c.detail}" if not c.passed and c.detail else "")
                                     for c in r.checks]
        out += ["", "<details><summary>Full trace ({} events)</summary>".format(len(res.get("trace", []))), "",
                "| # | Component | Rule | Outcome | Effect | Summary | Ref | Conf. factor |", "|---|---|---|---|---|---|---|---|"]
        for e in res.get("trace", []):
            summary = (e.get("summary") or "").replace("|", "\\|").replace("\n", " ")
            ref = e.get("policy_ref") or e.get("interpretation_ref") or ""
            factor = f"{e['confidence_factor']} ({e['confidence_scope']})" if e.get("confidence_factor") is not None else ""
            out.append(f"| {e['seq']} | {e['component']} | {e.get('rule_id') or ''} | {e['outcome']} | {e['effect']} | "
                       f"{summary} | {ref} | {factor} |")
        out += ["", "</details>"]
        if not r.passed:
            out += ["", "**Why it did not match:** see the failing checks above."]
    return "\n".join(out) + "\n"
