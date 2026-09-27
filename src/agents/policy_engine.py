"""Policy engine: deterministic adjudication of one claim against policy_terms.json.

Component contract
-------------------
Input:  ClaimSubmission, MemberContext, List[DocumentExtraction] (usable ones), TraceLedger.
Output: PolicyResult
          proposal          APPROVED | PARTIAL | REJECTED | MANUAL_REVIEW
                            (a proposal: the DecisionAgent combines it with fraud and
                            degradation signals to produce the final decision)
          approved_amount   payable amount after every rule (0 when REJECTED)
          findings          one Finding per rejection / review reason:
                            code, kind (REJECT/REVIEW), ops text, member text, policy_ref
          line_items        per bill line: APPROVED / REJECTED / NEEDS_REVIEW + reason code
          financial         FinancialBreakdown with every arithmetic step
          warnings          non-blocking observations for the reviewer
          actions           member actions (e.g. OBTAIN_PRE_AUTH)
Errors: none by design for valid inputs. It is pure Decimal arithmetic over typed
        config. An unexpected exception is caught by run_stage (critical stage),
        which routes the claim to MANUAL_REVIEW with the trace intact.

Every rule is logged PASS / FAIL / WARN / SKIP / NOT_EVALUABLE with a plain-English
summary and the exact policy_terms.json path (policy_ref) or interpretation.json
key (interpretation_ref) it evaluated. A clean approval therefore shows that
waiting periods, exclusions and limits were checked, not just that nothing failed.

Order of evaluation (all claim-level rules run, so a rejection lists EVERY reason,
not just the first one hit; v1 failed fast on a single reason):
  1. category covered, minimum amount, submission deadline, policy status/period,
     member coverage date, relationship covered (family floater), initial waiting period,
     pre-existing (NOT_EVALUABLE, documented)
  2. condition exclusions on the diagnosis/treatment      -> EXCLUDED_CONDITION
  3. condition waiting periods (unless superseded by 2)   -> WAITING_PERIOD + eligible date
     Steps 2-3 match whole-phrase keywords (interpretation.json). A condition the vision model
     tagged (closed vocabulary) WITHOUT a keyword match routes to review
     (CLINICAL_CODING_UNCERTAIN): the LLM can raise a question, never deny a claim.
     Matching is negation-aware: "No family history of diabetes" is logged as a negated
     mention (SKIP) and does not trigger the diabetes waiting period.
     A prescription-requiring category with no readable diagnosis or treatment cannot be
     checked for exclusions or waiting periods -> review (DIAGNOSIS_UNREADABLE).
  4. category-specific rules (alt-med practitioner/system/sessions, dental report)
  5. per bill line: exclusion keywords, category procedure lists, pre-authorisation
  6. per-claim ceiling on the eligible amount             -> PER_CLAIM_EXCEEDED
  7. payout waterfall per line: network discount -> co-pay (branded pharmacy items
     use branded_drug_copay_percent) -> annual OPD limit (per patient) -> family floater
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Callable, Dict, Iterable, List, Literal, Optional, Tuple

from src.agents.intake import MemberContext
from src.core.text import (
    classify_registration, expand_abbreviations, find_mentions, fmt_date, fmt_inr, humanize, keyword_hits, normalize_text,
    q2,
)
from src.models.claim import ClaimSubmission, FinancialBreakdown, LineItemDecision, MemberAction
from src.models.medical import BILL_TYPES, DocumentExtraction
from src.models.policy import CategoryRules, Interpretation, PolicyConfig
from src.models.trace import Stopwatch, TraceLedger

COMPONENT = "POLICY_ENGINE"
ZERO = Decimal(0)
HUNDRED = Decimal(100)
_GENERIC_PROVIDER_WORDS = {"hospital", "hospitals", "healthcare", "health", "clinic", "clinics", "the", "ltd", "pvt", "limited"}

# paid_between(patient_ids, start, end, exclude_claim_id) -> Decimal, e.g. ClaimStore.paid_between
UsageLookup = Callable[[Iterable[str], date, date, str], Decimal]


@dataclass
class Finding:
    code: str
    kind: Literal["REJECT", "REVIEW"]
    ops_text: str
    member_text: str
    policy_ref: Optional[str] = None


@dataclass
class _Line:
    description: str
    amount: Decimal
    source_file_id: Optional[str]
    synthetic: bool = False
    is_branded: Optional[bool] = None
    status: str = "APPROVED"
    reason: Optional[str] = None
    reason_code: Optional[str] = None
    policy_ref: Optional[str] = None
    pre_authorized: bool = False

    def reject(self, code: str, reason: str, ref: Optional[str]) -> None:
        if self.status != "REJECTED":  # the first rejection reason is the one reported
            self.status, self.reason_code, self.reason, self.policy_ref = "REJECTED", code, reason, ref

    def review(self, code: str, reason: str, ref: Optional[str]) -> None:
        if self.status == "APPROVED":
            self.status, self.reason_code, self.reason, self.policy_ref = "NEEDS_REVIEW", code, reason, ref

    def to_model(self) -> LineItemDecision:
        return LineItemDecision(description=self.description, amount=q2(self.amount), status=self.status,
                                reason=self.reason, reason_code=self.reason_code, policy_ref=self.policy_ref,
                                source_file_id=self.source_file_id)


@dataclass
class PolicyResult:
    proposal: str
    approved_amount: Decimal = ZERO
    findings: List[Finding] = field(default_factory=list)
    line_items: List[LineItemDecision] = field(default_factory=list)
    financial: Optional[FinancialBreakdown] = None
    warnings: List[str] = field(default_factory=list)
    actions: List[MemberAction] = field(default_factory=list)

    @property
    def rejection_codes(self) -> List[str]:
        return list(dict.fromkeys(f.code for f in self.findings if f.kind == "REJECT"))

    @property
    def review_codes(self) -> List[str]:
        return list(dict.fromkeys(f.code for f in self.findings if f.kind == "REVIEW"))


class PolicyEngine:
    def __init__(self, policy: PolicyConfig, interpretation: Interpretation, usage: Optional[UsageLookup] = None):
        self.policy = policy
        self.interp = interpretation
        self.weights = interpretation.confidence
        self.usage = usage

    # ================================================================ entry point

    def evaluate(self, submission: ClaimSubmission, context: MemberContext, extractions: List[DocumentExtraction],
                 ledger: TraceLedger) -> PolicyResult:
        watch = Stopwatch()
        category = submission.category
        rules = self.policy.category_rules(category)
        findings: List[Finding] = []
        warnings: List[str] = []
        actions: List[MemberAction] = []
        usable = [e for e in extractions if e.usable]

        # -------- 1. claim-level eligibility
        if rules is None or not rules.covered:
            findings.append(self._reject(
                ledger, "category_covered", "CATEGORY_NOT_COVERED", f"opd_categories.{category.lower()}",
                f"{humanize(category).capitalize()} treatment is not covered under {self.policy.policy_name}.",
                evidence={"category": category}))
            return self._finish(ledger, watch, submission, findings, [], None, warnings, actions)
        ledger.log(COMPONENT, "PASS", rule_id="category_covered", policy_ref=f"opd_categories.{category.lower()}.covered",
                   summary=f"{humanize(category).capitalize()} is a covered OPD category.")

        self._check_minimum(submission, ledger, findings)
        self._check_deadline(submission, ledger, findings)
        self._check_policy_period(submission, ledger, findings)
        self._check_member_dates(submission, context, ledger, findings)
        self._check_relationship(context, ledger, findings)
        self._check_initial_waiting(submission, context, ledger, findings)
        ledger.log(COMPONENT, "NOT_EVALUABLE", rule_id="pre_existing_conditions",
                   policy_ref="waiting_periods.pre_existing_conditions_days",
                   interpretation_ref="assumptions.pre_existing_conditions",
                   summary="Pre-existing condition waiting period not evaluated: distinguishing a pre-existing condition "
                           "needs medical history this system does not receive (documented assumption).")

        # -------- 2/3. clinical rules on diagnosis + treatment text
        clinical = self._clinical_text(usable)
        self._check_diagnosis_present(rules, clinical, submission.category, usable, ledger, findings)
        excluded_conditions = self._check_condition_exclusions(clinical, ledger, findings)
        self._check_condition_waiting(submission, context, clinical, usable, excluded_conditions, ledger, findings)

        # -------- 4. category-specific rules
        self._check_category_specific(submission, rules, usable, clinical, ledger, findings, warnings)

        # -------- 5. line items
        lines = self._build_lines(submission, usable, ledger, warnings)
        claim_rejected = any(f.kind == "REJECT" for f in findings)
        if claim_rejected:
            codes = list(dict.fromkeys(f.code for f in findings if f.kind == "REJECT"))
            for line in lines:
                line.reject(codes[0], f"Whole claim rejected ({', '.join(codes)}).", None)
            return self._finish(ledger, watch, submission, findings, lines, None, warnings, actions)

        self._evaluate_lines(submission, category, rules, lines, clinical, ledger, findings, actions)

        # -------- 6. per-claim ceiling
        approved_lines = [l for l in lines if l.status == "APPROVED"]
        approved_sum = sum((l.amount for l in approved_lines), ZERO)
        eligible = min(approved_sum, submission.claimed_amount)
        if approved_lines and self._check_per_claim(submission, rules, approved_lines, approved_sum, eligible, ledger, findings, warnings):
            for line in approved_lines:
                line.reject("PER_CLAIM_EXCEEDED", "Claim exceeds the per-claim limit.", "coverage.per_claim_limit")
            return self._finish(ledger, watch, submission, findings, lines, None, warnings, actions)

        if not approved_lines:
            return self._finish(ledger, watch, submission, findings, lines, None, warnings, actions)

        # -------- 7. payout waterfall
        financial = self._waterfall(submission, context, rules, lines, approved_sum, eligible, usable, ledger, findings)
        return self._finish(ledger, watch, submission, findings, lines, financial, warnings, actions)

    # ================================================================ claim-level rules

    def _check_minimum(self, submission, ledger, findings):
        minimum = self.policy.submission_rules.minimum_claim_amount
        ref = "submission_rules.minimum_claim_amount"
        if submission.claimed_amount < minimum:
            findings.append(self._reject(
                ledger, "minimum_claim_amount", "BELOW_MINIMUM_AMOUNT", ref,
                f"The claimed amount {fmt_inr(submission.claimed_amount)} is below the minimum claim amount of "
                f"{fmt_inr(minimum)}.", evidence={"claimed": str(submission.claimed_amount), "minimum": str(minimum)}))
        else:
            ledger.log(COMPONENT, "PASS", rule_id="minimum_claim_amount", policy_ref=ref,
                       summary=f"Claimed {fmt_inr(submission.claimed_amount)} meets the {fmt_inr(minimum)} minimum.")

    def _check_deadline(self, submission, ledger, findings):
        days = self.policy.submission_rules.deadline_days_from_treatment
        ref = "submission_rules.deadline_days_from_treatment"
        if submission.submission_date is None:
            ledger.log(COMPONENT, "NOT_EVALUABLE", rule_id="submission_deadline", policy_ref=ref,
                       interpretation_ref="assumptions.submission_deadline",
                       summary=f"No submission date supplied, so the {days}-day submission deadline could not be checked.")
            return
        elapsed = (submission.submission_date - submission.treatment_date).days
        deadline = submission.treatment_date + timedelta(days=days)
        if elapsed > days:
            findings.append(self._reject(
                ledger, "submission_deadline", "SUBMISSION_DEADLINE_EXCEEDED", ref,
                f"Claims must be submitted within {days} days of treatment (by {fmt_date(deadline)}); this claim was "
                f"submitted on {fmt_date(submission.submission_date)}, {elapsed} days after treatment.",
                evidence={"elapsed_days": elapsed, "deadline": deadline.isoformat()}))
        else:
            ledger.log(COMPONENT, "PASS", rule_id="submission_deadline", policy_ref=ref,
                       summary=f"Submitted {elapsed} day(s) after treatment, within the {days}-day deadline.")

    def _check_policy_period(self, submission, ledger, findings):
        holder = self.policy.policy_holder
        if holder.renewal_status.upper() != "ACTIVE":
            findings.append(self._reject(
                ledger, "policy_status", "POLICY_INACTIVE", "policy_holder.renewal_status",
                f"The group policy {self.policy.policy_id} is not active (status: {holder.renewal_status}).",
                evidence={"renewal_status": holder.renewal_status}))
        else:
            ledger.log(COMPONENT, "PASS", rule_id="policy_status", policy_ref="policy_holder.renewal_status",
                       summary=f"Policy {self.policy.policy_id} is ACTIVE.")
        start, end = holder.policy_start_date, holder.policy_end_date
        ref = "policy_holder.policy_start_date..policy_end_date"
        if not (start <= submission.treatment_date <= end):
            findings.append(self._reject(
                ledger, "policy_period", "OUTSIDE_POLICY_PERIOD", ref,
                f"The treatment date {fmt_date(submission.treatment_date)} is outside the policy period "
                f"{fmt_date(start)} to {fmt_date(end)}.",
                evidence={"treatment_date": submission.treatment_date.isoformat(), "start": start.isoformat(), "end": end.isoformat()}))
        else:
            ledger.log(COMPONENT, "PASS", rule_id="policy_period", policy_ref=ref,
                       summary=f"Treatment on {fmt_date(submission.treatment_date)} falls within the policy period "
                               f"{fmt_date(start)} to {fmt_date(end)}.")

    def _check_member_dates(self, submission, context: MemberContext, ledger, findings):
        if submission.treatment_date < context.coverage_start:
            findings.append(self._reject(
                ledger, "member_coverage_date", "MEMBER_NOT_COVERED_ON_DATE", "members[].join_date",
                f"The patient's cover started on {fmt_date(context.coverage_start)}, after the treatment date "
                f"{fmt_date(submission.treatment_date)}.",
                evidence={"coverage_start": context.coverage_start.isoformat()}))
        else:
            ledger.log(COMPONENT, "PASS", rule_id="member_coverage_date", policy_ref="members[].join_date",
                       summary=f"Patient was covered on the treatment date (cover started {fmt_date(context.coverage_start)}).")

    def _check_relationship(self, context: MemberContext, ledger, findings):
        floater = self.policy.coverage.family_floater
        ref = "coverage.family_floater.covered_relationships"
        if context.roster_incomplete:
            ledger.log(COMPONENT, "NOT_EVALUABLE", effect="REVIEW", rule_id="relationship_covered", policy_ref=ref,
                       summary=f"{context.patient_id} has no roster record, so their relationship to the member cannot be "
                               f"confirmed; claim needs manual review.")
            findings.append(Finding("ROSTER_INCOMPLETE", "REVIEW",
                                    f"Dependent {context.patient_id} is linked to {context.member.member_id} but has no roster record.",
                                    "We need to confirm the patient's details on your policy; our team will review this claim.",
                                    ref))
            return
        raw = context.relationship.upper()
        canonical = self.interp.relationship_aliases.get(raw, raw)
        covered = [self.interp.relationship_aliases.get(r.upper(), r.upper()) for r in floater.covered_relationships]
        if canonical == "SELF" or (floater.enabled and canonical in covered):
            ledger.log(COMPONENT, "PASS", rule_id="relationship_covered", policy_ref=ref,
                       interpretation_ref="relationship_aliases",
                       summary=f"Relationship {raw} (policy term {canonical}) is covered.",
                       evidence={"relationship": raw, "canonical": canonical, "covered": floater.covered_relationships})
            return
        findings.append(self._reject(
            ledger, "relationship_covered", "RELATIONSHIP_NOT_COVERED", ref,
            f"The patient's relationship to the employee ({raw.lower()}) is not covered under this policy "
            f"(covered: {', '.join(r.lower() for r in floater.covered_relationships) or 'employee only'}).",
            evidence={"relationship": raw, "canonical": canonical, "floater_enabled": floater.enabled}))

    def _check_initial_waiting(self, submission, context: MemberContext, ledger, findings):
        days = self.policy.waiting_periods.initial_waiting_period_days
        eligible = context.coverage_start + timedelta(days=days)
        ref = "waiting_periods.initial_waiting_period_days"
        if submission.treatment_date < eligible:
            findings.append(self._reject(
                ledger, "initial_waiting_period", "WAITING_PERIOD", ref,
                f"Treatment on {fmt_date(submission.treatment_date)} is within the {days}-day initial waiting period "
                f"that started on {fmt_date(context.coverage_start)}. Claims are eligible from {fmt_date(eligible)}.",
                evidence={"coverage_start": context.coverage_start.isoformat(), "eligible_from": eligible.isoformat()}))
        else:
            ledger.log(COMPONENT, "PASS", rule_id="initial_waiting_period", policy_ref=ref,
                       summary=f"Initial {days}-day waiting period ended {fmt_date(eligible)}, before treatment.")

    # ================================================================ clinical rules

    def _clinical_text(self, usable: List[DocumentExtraction]) -> Dict[str, str]:
        diag, meds, tests, tags = [], [], [], []
        for e in usable:
            if e.prescription_data:
                tags += e.prescription_data.condition_tags
                diag += e.prescription_data.diagnoses + e.prescription_data.treatments
                meds += e.prescription_data.medicines
                tests += e.prescription_data.tests_ordered
            if e.report_data:
                tests += e.report_data.tests
        abbreviations = self.interp.medical_abbreviations
        return {
            "diagnosis": expand_abbreviations(" ; ".join(diag), abbreviations),
            "medicines": " ; ".join(meds),
            "tests": " ; ".join(tests),
            "tags": " ; ".join(dict.fromkeys(tags)),
        }

    @staticmethod
    def _tagged(clinical, name: str) -> bool:
        return name.lower() in {t.strip().lower() for t in clinical["tags"].split(" ; ") if t.strip()}

    def _llm_only(self, ledger, findings, rule_id, ref, name, what) -> None:
        ledger.log(COMPONENT, "WARN", effect="REVIEW", rule_id=rule_id, policy_ref=ref,
                   interpretation_ref="assumptions.clinical_coding",
                   summary=f"The vision model tagged this claim as '{name}' but no keyword in the diagnosis/treatment "
                           f"confirms it; {what}. Routed to a human instead of deciding on a model inference alone.",
                   evidence={"condition_tag": name},
                   confidence_factor=self.weights.llm_only_clinical_code, confidence_scope="ALL")
        findings.append(Finding("CLINICAL_CODING_UNCERTAIN", "REVIEW",
                                f"Model-only clinical code '{name}' ({what}).",
                                "Our medical team needs to confirm the diagnosis on your documents.", ref))

    def _mentions(self, text: str, keywords: List[str]):
        return find_mentions(text, keywords, self.interp.negation_cues, self.interp.negation_window_words)

    def _check_diagnosis_present(self, rules: CategoryRules, clinical, category: str, usable, ledger, findings) -> None:
        if clinical["diagnosis"] or clinical["tags"]:
            return
        ref = f"opd_categories.{category.lower()}.requires_prescription"
        if not any(e.prescription_data is not None for e in usable):
            # No prescription was extracted at all (e.g. a system failure, already routed to review as
            # EXTRACTION_INCOMPLETE). Saying the diagnosis was "unreadable" would misstate the cause.
            ledger.log(COMPONENT, "NOT_EVALUABLE", rule_id="diagnosis_present", policy_ref=ref,
                       summary="No extracted prescription is available, so the diagnosis could not be checked.")
            return
        if not rules.requires_prescription:
            ledger.log(COMPONENT, "NOT_EVALUABLE", rule_id="diagnosis_present", policy_ref=ref,
                       summary=f"No diagnosis on the documents; {humanize(category)} does not require a prescription, so "
                               f"items are assessed from the bill.")
            return
        ledger.log(COMPONENT, "WARN", effect="REVIEW", rule_id="diagnosis_present", policy_ref=ref,
                   interpretation_ref="assumptions.diagnosis_required",
                   summary="No diagnosis or treatment could be read on the prescription, so exclusions and waiting "
                           "periods cannot be checked; the claim needs a human review.",
                   confidence_factor=self.weights.diagnosis_missing, confidence_scope="ALL")
        findings.append(Finding("DIAGNOSIS_UNREADABLE", "REVIEW",
                                "No readable diagnosis or treatment; exclusions and waiting periods not checkable.",
                                "We couldn't read the diagnosis on your prescription, so our medical team will review "
                                "the claim.", ref))

    def _check_condition_exclusions(self, clinical, ledger, findings) -> List[str]:
        """Exclusions matched on the diagnosis/treatment reject the whole claim."""
        text = clinical["diagnosis"]
        if not text and not clinical["tags"]:
            ledger.log(COMPONENT, "NOT_EVALUABLE", rule_id="exclusions", policy_ref="exclusions.conditions",
                       summary="No diagnosis or treatment could be read, so condition exclusions were checked on bill "
                               "line items only.")
            return []
        matched: List[str] = []
        rejecting: List[Tuple[str, List[str]]] = []
        negated: Dict[str, Dict[str, str]] = {}
        for exclusion in self.policy.exclusions.conditions:
            rule = self.interp.exclusion_condition_keywords.get(exclusion)
            if rule is None:
                continue
            mentions = self._mentions(text, rule.keywords)
            hits = mentions.hits
            if mentions.negated and not hits:
                negated[exclusion] = mentions.negated
            if not hits:
                if self._tagged(clinical, exclusion):
                    matched.append(exclusion)
                    self._llm_only(ledger, findings, "exclusions", "exclusions.conditions", exclusion,
                                   "it would be a policy exclusion")
                continue
            matched.append(exclusion)
            if rule.on_match == "REJECT":
                rejecting.append((exclusion, hits))
                continue
            ledger.log(COMPONENT, "WARN", effect="REVIEW", rule_id="exclusions", policy_ref="exclusions.conditions",
                       interpretation_ref=f"exclusion_condition_keywords.{exclusion}",
                       summary=f"Diagnosis/treatment mentions {', '.join(hits)}, which may fall under the exclusion "
                               f"'{exclusion}'; medical necessity needs a human decision.",
                       evidence={"exclusion": exclusion, "matched": hits})
            findings.append(Finding("ITEM_NEEDS_REVIEW", "REVIEW",
                                    f"Possible exclusion '{exclusion}' (matched {', '.join(hits)}).",
                                    f"Part of this claim ({exclusion.lower()}) needs a medical-necessity review.",
                                    "exclusions.conditions"))
        if rejecting:
            names = " and ".join(f"'{name}'" for name, _ in rejecting)
            all_hits = [h for _, hits in rejecting for h in hits]
            # One event, one confidence factor: several exclusions matching is MORE evidence, not less.
            findings.append(self._reject(
                ledger, "exclusions", "EXCLUDED_CONDITION", "exclusions.conditions",
                f"The diagnosis/treatment ({text}) falls under the policy exclusion{'s' if len(rejecting) > 1 else ''} "
                f"{names} (matched: {', '.join(all_hits)}). Excluded conditions are not covered at any point in the "
                f"policy, so the whole claim is rejected.",
                evidence={"exclusions": {name: hits for name, hits in rejecting}, "text": text},
                interpretation_ref="exclusion_condition_keywords",
                confidence_factor=self.weights.keyword_clinical_match))
        if not matched:
            skipped = "; ".join(f"'{snip}'" for m in negated.values() for snip in m.values())
            ledger.log(COMPONENT, "PASS", rule_id="exclusions", policy_ref="exclusions.conditions",
                       interpretation_ref="exclusion_condition_keywords",
                       summary=f"Diagnosis/treatment '{text}' matches none of the "
                               f"{len(self.policy.exclusions.conditions)} excluded conditions"
                               + (f" (negated mentions ignored: {skipped})." if skipped else "."),
                       evidence={"negated_mentions": negated} if negated else {})
        return matched

    def _check_condition_waiting(self, submission, context: MemberContext, clinical, usable, excluded_conditions,
                                 ledger, findings) -> None:
        specific = self.policy.waiting_periods.specific_conditions
        text = clinical["diagnosis"]
        excluded_keywords = set()
        for exclusion in excluded_conditions:
            rule = self.interp.exclusion_condition_keywords.get(exclusion)
            if rule and rule.on_match == "REJECT":
                excluded_keywords.update(normalize_text(k) for k in rule.keywords)
        any_hit = False
        factor_used = False
        for condition, days in specific.items():
            keywords = self.interp.waiting_period_condition_keywords.get(condition, [])
            mentions = self._mentions(text, keywords)
            hits = mentions.hits
            hints = keyword_hits(clinical["medicines"], self.interp.medicine_condition_hints.get(condition, []))
            eligible = context.coverage_start + timedelta(days=days)
            ref = f"waiting_periods.specific_conditions.{condition}"
            iref = f"waiting_period_condition_keywords.{condition}"
            if not hits and not hints and mentions.negated and not self._tagged(clinical, condition):
                any_hit = True
                ledger.log(COMPONENT, "SKIP", rule_id="condition_waiting_period", policy_ref=ref,
                           interpretation_ref="assumptions.negation",
                           summary=f"{humanize(condition).capitalize()} is mentioned only in a negated context "
                                   f"({'; '.join(repr(v) for v in mentions.negated.values())}), so it is not treated as "
                                   f"a diagnosis and its waiting period does not apply.",
                           evidence={"condition": condition, "negated_mentions": mentions.negated})
                continue
            if not hits and not hints:
                if self._tagged(clinical, condition):
                    any_hit = True
                    superseded = excluded_keywords.intersection(normalize_text(k) for k in keywords)
                    if submission.treatment_date < eligible and not superseded:
                        self._llm_only(ledger, findings, "condition_waiting_period", ref, condition,
                                       f"it would be inside its {days}-day waiting period (eligible from {fmt_date(eligible)})")
                    else:
                        ledger.log(COMPONENT, "PASS", rule_id="condition_waiting_period", policy_ref=ref,
                                   summary=f"Model tagged {humanize(condition)}; its waiting period (ended "
                                           f"{fmt_date(eligible)}) does not affect this claim.")
                continue
            any_hit = True
            if hits and excluded_keywords.intersection(normalize_text(h) for h in hits):
                ledger.log(COMPONENT, "SKIP", rule_id="condition_waiting_period", policy_ref=ref,
                           interpretation_ref="assumptions.exclusion_precedence",
                           summary=f"{humanize(condition).capitalize()} waiting period not applied: the condition is "
                                   f"permanently excluded, which supersedes a waiting period.",
                           evidence={"condition": condition, "matched": hits})
                continue
            if submission.treatment_date >= eligible:
                ledger.log(COMPONENT, "PASS", rule_id="condition_waiting_period", policy_ref=ref, interpretation_ref=iref,
                           summary=f"{humanize(condition).capitalize()} ({', '.join(hits or hints)}) has a {days}-day "
                                   f"waiting period, which ended {fmt_date(eligible)}.",
                           evidence={"condition": condition, "matched": hits, "medicine_hints": hints,
                                     "eligible_from": eligible.isoformat()})
                continue
            if hits:
                findings.append(self._reject(
                    ledger, "condition_waiting_period", "WAITING_PERIOD", ref,
                    f"The diagnosis indicates {humanize(condition)} ({', '.join(hits)}), which has a {days}-day waiting "
                    f"period from the start of cover ({fmt_date(context.coverage_start)}). Treatment on "
                    f"{fmt_date(submission.treatment_date)} is inside it. You will be eligible for "
                    f"{humanize(condition)}-related claims from {fmt_date(eligible)}.",
                    evidence={"condition": condition, "matched": hits, "medicine_hints": hints, "days": days,
                              "coverage_start": context.coverage_start.isoformat(), "eligible_from": eligible.isoformat()},
                    interpretation_ref=iref,
                    confidence_factor=None if factor_used else self.weights.keyword_clinical_match))
                factor_used = True
            else:
                ledger.log(COMPONENT, "WARN", rule_id="condition_waiting_period", policy_ref=ref,
                           interpretation_ref=f"medicine_condition_hints.{condition}",
                           summary=f"Prescribed medicine(s) {', '.join(hints)} are typically used for "
                                   f"{humanize(condition)}, which is still in its {days}-day waiting period (until "
                                   f"{fmt_date(eligible)}), but the diagnosis does not name it. Not rejected; flagged.",
                           evidence={"condition": condition, "medicine_hints": hints, "eligible_from": eligible.isoformat()},
                           confidence_factor=self.weights.medicine_condition_hint, confidence_scope="PAYOUT")
        if not any_hit:
            ledger.log(COMPONENT, "PASS", rule_id="condition_waiting_period", policy_ref="waiting_periods.specific_conditions",
                       interpretation_ref="waiting_period_condition_keywords",
                       summary=f"No condition with a specific waiting period ({', '.join(humanize(c) for c in specific)}) "
                               f"was found in the diagnosis or medicines." if text or clinical["medicines"] else
                               "No diagnosis or medicines could be read; specific waiting periods could not be matched.")

    # ================================================================ category-specific

    def _check_category_specific(self, submission, rules: CategoryRules, usable, clinical, ledger, findings, warnings):
        cat = submission.category.lower()
        prescriptions = [e for e in usable if e.prescription_data]
        if rules.requires_registered_practitioner:
            ref = f"opd_categories.{cat}.requires_registered_practitioner"
            allowed = {name: self.interp.registration_patterns[name]
                       for name in self.interp.alternative_medicine_registration_patterns
                       if name in self.interp.registration_patterns}
            regs = [(e, e.prescription_data.registration_number) for e in prescriptions]
            verified = [(e, r) for e, r in regs if classify_registration(r, allowed)[0]]
            if verified:
                ledger.log(COMPONENT, "PASS", rule_id="registered_practitioner", policy_ref=ref,
                           interpretation_ref="alternative_medicine_registration_patterns",
                           summary=f"Practitioner registration {verified[0][1]} matches a recognised AYUSH format.",
                           evidence={"registration": verified[0][1]})
            else:
                shown = ", ".join(r for _, r in regs if r) or "none found"
                ledger.log(COMPONENT, "WARN", effect="REVIEW", rule_id="registered_practitioner", policy_ref=ref,
                           summary=f"A registered practitioner is required, but no recognised AYUSH registration was "
                                   f"found (registration on documents: {shown}).", evidence={"registrations": shown})
                findings.append(Finding("PRACTITIONER_UNVERIFIED", "REVIEW",
                                        f"Practitioner registration not verifiable ({shown}).",
                                        "We need to verify your practitioner's registration; our team will review this claim.",
                                        ref))

        if rules.covered_systems:
            ref = f"opd_categories.{cat}.covered_systems"
            corpus = " ; ".join([clinical["diagnosis"], clinical["medicines"]] +
                                [e.prescription_data.registration_number or "" for e in prescriptions] +
                                [e.prescription_data.doctor_name or "" for e in prescriptions] +
                                [li.description for e in usable if e.bill_data for li in e.bill_data.line_items] +
                                [e.bill_data.hospital_name or "" for e in usable if e.bill_data])
            detected = [system for system, kws in self.interp.alternative_medicine_system_keywords.items()
                        if keyword_hits(corpus, kws)]
            covered = {s.lower() for s in rules.covered_systems}
            if not detected:
                ledger.log(COMPONENT, "NOT_EVALUABLE", rule_id="covered_system", policy_ref=ref,
                           interpretation_ref="alternative_medicine_system_keywords",
                           summary="Could not tell which system of medicine was used; covered-system check not evaluated.")
                warnings.append("System of alternative medicine could not be identified from the documents.")
            elif any(d.lower() in covered for d in detected):
                ledger.log(COMPONENT, "PASS", rule_id="covered_system", policy_ref=ref,
                           interpretation_ref="alternative_medicine_system_keywords",
                           summary=f"Treatment identified as {', '.join(detected)}, a covered system.",
                           evidence={"detected": detected, "covered": rules.covered_systems})
            else:
                findings.append(self._reject(
                    ledger, "covered_system", "CATEGORY_NOT_COVERED", ref,
                    f"The treatment appears to be {', '.join(detected)}, which is not a covered system "
                    f"(covered: {', '.join(rules.covered_systems)}).", evidence={"detected": detected}))

        if rules.max_sessions_per_year:
            ref = f"opd_categories.{cat}.max_sessions_per_year"
            texts = [li.description for e in usable if e.bill_data for li in e.bill_data.line_items]
            if not texts:
                texts = [t for e in prescriptions for t in e.prescription_data.treatments]
            pattern = re.compile(self.interp.session_count_pattern, re.IGNORECASE)
            sessions = sum(int(m.group(1)) for t in texts for m in pattern.finditer(t))
            if sessions == 0:
                ledger.log(COMPONENT, "NOT_EVALUABLE", rule_id="session_limit", policy_ref=ref,
                           summary="No session count found on the documents; session limit not evaluated for this claim.")
            elif sessions > rules.max_sessions_per_year:
                ledger.log(COMPONENT, "WARN", effect="REVIEW", rule_id="session_limit", policy_ref=ref,
                           summary=f"This claim alone bills {sessions} sessions, above the yearly maximum of "
                                   f"{rules.max_sessions_per_year}.", evidence={"sessions": sessions})
                findings.append(Finding("SESSION_LIMIT_EXCEEDED", "REVIEW",
                                        f"{sessions} sessions billed; yearly maximum is {rules.max_sessions_per_year}.",
                                        f"The number of sessions billed ({sessions}) is above the yearly limit of "
                                        f"{rules.max_sessions_per_year}; our team will review this claim.", ref))
            else:
                ledger.log(COMPONENT, "PASS", rule_id="session_limit", policy_ref=ref,
                           summary=f"{sessions} session(s) billed, within the yearly maximum of {rules.max_sessions_per_year} "
                                   f"(prior sessions this year are not tracked; documented limitation).",
                           evidence={"sessions": sessions})

        if rules.requires_dental_report:
            ref = f"opd_categories.{cat}.requires_dental_report"
            if any(e.effective_type == "DENTAL_REPORT" for e in usable):
                ledger.log(COMPONENT, "PASS", rule_id="dental_report", policy_ref=ref, summary="Dental report provided.")
            else:
                ledger.log(COMPONENT, "WARN", rule_id="dental_report", policy_ref=ref,
                           interpretation_ref="confidence.missing_supporting_report",
                           summary="The policy expects a dental report, but none was uploaded (it is optional in "
                                   "document_requirements, so the claim is not blocked; procedures are taken from the bill).",
                           confidence_factor=self.weights.missing_supporting_report, confidence_scope="PAYOUT")
                warnings.append("No dental report uploaded; procedures verified from the bill only.")

    # ================================================================ line items

    def _build_lines(self, submission, usable, ledger, warnings) -> List[_Line]:
        lines: List[_Line] = []
        bills = [e for e in usable if e.effective_type in BILL_TYPES and e.bill_data is not None]
        for bill in bills:
            data = bill.bill_data
            if data.line_items:
                items = [_Line(li.description, li.amount, bill.file_id, is_branded=li.is_branded)
                         for li in data.line_items]
                lines += items
                line_sum = sum((i.amount for i in items), ZERO)
                if data.total_amount is not None and abs(line_sum - data.total_amount) > Decimal("1"):
                    ledger.log(COMPONENT, "WARN", rule_id="bill_arithmetic", interpretation_ref="confidence.bill_total_mismatch",
                               summary=f"Line items on '{bill.label}' add up to {fmt_inr(line_sum)} but the bill total says "
                                       f"{fmt_inr(data.total_amount)}; line items are used.",
                               evidence={"line_sum": str(line_sum), "total": str(data.total_amount)},
                               confidence_factor=self.weights.bill_total_mismatch, confidence_scope="PAYOUT")
                    warnings.append(f"Bill '{bill.label}' total does not match its line items.")
            elif data.total_amount is not None:
                lines.append(_Line(f"Total billed on '{bill.label}' (not itemised)", data.total_amount, bill.file_id,
                                   synthetic=True))
        if not lines:
            lines.append(_Line("Claimed amount (no readable bill)", submission.claimed_amount, None, synthetic=True))
            ledger.log(COMPONENT, "WARN", effect="REVIEW", rule_id="billed_amount",
                       summary="No bill amounts were available; the member-entered claimed amount is used provisionally.",
                       evidence={"claimed": str(submission.claimed_amount)})
            warnings.append("No readable bill amounts; claimed amount used provisionally.")
        else:
            billed = sum((l.amount for l in lines), ZERO)
            ledger.log(COMPONENT, "INFO", rule_id="billed_amount",
                       summary=f"{len(lines)} bill line(s) from {len(bills)} bill(s), totalling {fmt_inr(billed)} "
                               f"(claimed {fmt_inr(submission.claimed_amount)}).",
                       evidence={"lines": [{"description": l.description, "amount": str(l.amount), "file_id": l.source_file_id}
                                           for l in lines]})
        return lines

    def _evaluate_lines(self, submission, category, rules: CategoryRules, lines: List[_Line], clinical, ledger,
                        findings, actions) -> None:
        cat = category.lower()
        extra_exclusions: List[str] = list(getattr(self.policy.exclusions, f"{cat}_exclusions", None) or [])
        excluded_names = rules.excluded_list + extra_exclusions
        covered_names = rules.covered_list
        threshold = rules.pre_auth_threshold
        if threshold is None:
            thresholds = [r.pre_auth_threshold for r in self.policy.opd_categories.values() if r.pre_auth_threshold is not None]
            threshold = min(thresholds) if thresholds else None
        pre_auth_missing: List[Tuple[_Line, str]] = []
        keyword_factor_used = False

        for line in lines:
            text = line.description + (f" ; {clinical['tests']}" if line.synthetic else "")

            # a) exclusion keywords on the line itself (e.g. "diet programme" on a consultation bill)
            for exclusion in self.policy.exclusions.conditions:
                rule = self.interp.exclusion_condition_keywords.get(exclusion)
                hits = self._mentions(line.description, rule.keywords).hits if rule else []
                if not hits:
                    continue
                reason = f"'{line.description}' falls under the exclusion '{exclusion}' (matched: {', '.join(hits)})."
                if rule.on_match == "REJECT":
                    line.reject("EXCLUDED_CONDITION", reason, "exclusions.conditions")
                else:
                    line.review("ITEM_NEEDS_REVIEW", reason + " Medical necessity needs review.", "exclusions.conditions")
                ledger.log(COMPONENT, "FAIL" if rule.on_match == "REJECT" else "WARN",
                           effect="REJECT" if rule.on_match == "REJECT" else "REVIEW", rule_id="line_exclusion",
                           policy_ref="exclusions.conditions", interpretation_ref=f"exclusion_condition_keywords.{exclusion}",
                           summary=reason, evidence={"line": line.description, "amount": str(line.amount), "matched": hits},
                           confidence_factor=(self.weights.keyword_clinical_match
                                              if rule.on_match == "REJECT" and not keyword_factor_used else None))
                keyword_factor_used = keyword_factor_used or rule.on_match == "REJECT"
                break

            # b) category procedure / item lists (dental, vision)
            if excluded_names or covered_names:
                excluded_hit = self._match_named(line.description, excluded_names)
                covered_hit = self._match_named(line.description, covered_names)
                if excluded_hit:
                    ref = (f"opd_categories.{cat}.excluded_{'procedures' if rules.excluded_procedures else 'items'}"
                           if excluded_hit in rules.excluded_list else f"exclusions.{cat}_exclusions")
                    reason = f"'{line.description}' is an excluded {humanize(cat)} procedure ({excluded_hit}) under the policy."
                    line.reject("EXCLUDED_PROCEDURE", reason, ref)
                    ledger.log(COMPONENT, "FAIL", effect="REJECT", rule_id="procedure_coverage", policy_ref=ref,
                               interpretation_ref="procedure_aliases", summary=reason,
                               evidence={"line": line.description, "amount": str(line.amount), "matched": excluded_hit})
                elif covered_hit:
                    ledger.log(COMPONENT, "PASS", rule_id="procedure_coverage",
                               policy_ref=f"opd_categories.{cat}.covered_{'procedures' if rules.covered_procedures else 'items'}",
                               interpretation_ref="procedure_aliases",
                               summary=f"'{line.description}' is a covered {humanize(cat)} item ({covered_hit}).",
                               evidence={"line": line.description, "matched": covered_hit})
                elif covered_names and not line.synthetic:
                    ledger.log(COMPONENT, "WARN", rule_id="procedure_coverage", interpretation_ref="confidence.unlisted_procedure",
                               summary=f"'{line.description}' is neither on the covered nor the excluded {humanize(cat)} list; "
                                       f"allowed, flagged for awareness.",
                               evidence={"line": line.description},
                               confidence_factor=self.weights.unlisted_procedure, confidence_scope="PAYOUT")

            # c) pre-authorisation, evaluated per line with that line's amount
            # All matching rules are considered and the strictest wins: "PET-CT" matches both the CT rule
            # (threshold) and the PET rule (always), and must be treated as PET.
            matching = [pa for pa in self.interp.pre_auth_rules if keyword_hits(text, pa.keywords)]
            if matching:
                pa = next((m for m in matching if m.always_required), matching[0])
                required = pa.always_required or (threshold is not None and line.amount > threshold)
                ref = "pre_authorization.required_for"
                evidence = {"line": line.description, "amount": str(line.amount), "test": pa.test,
                            "matched_rules": [m.test for m in matching], "threshold": str(threshold),
                            "always_required": pa.always_required}
                if not required and threshold is None:
                    ledger.log(COMPONENT, "NOT_EVALUABLE", rule_id="pre_authorization", policy_ref=ref,
                               summary=f"'{line.description}' is a {pa.test}, but no pre-authorisation threshold is "
                                       f"configured for this category.", evidence=evidence)
                elif not required:
                    ledger.log(COMPONENT, "PASS", rule_id="pre_authorization", policy_ref=ref,
                               summary=f"'{line.description}' is a {pa.test} of {fmt_inr(line.amount)}, not above the "
                                       f"{fmt_inr(threshold)} pre-authorisation threshold.", evidence=evidence)
                elif submission.pre_auth_id:
                    line.pre_authorized = True
                    ledger.log(COMPONENT, "PASS", rule_id="pre_authorization", policy_ref=ref,
                               interpretation_ref="assumptions.pre_auth_validity",
                               summary=f"'{line.description}' ({pa.test}, {fmt_inr(line.amount)}) requires pre-authorisation; "
                                       f"pre-auth ID {submission.pre_auth_id} supplied.",
                               evidence={**evidence, "pre_auth_id": submission.pre_auth_id})
                else:
                    why = "always requires" if pa.always_required else f"above {fmt_inr(threshold)} requires"
                    reason = f"{pa.test} {why} pre-authorisation; none was obtained."
                    line.reject("PRE_AUTH_MISSING", reason, ref)
                    pre_auth_missing.append((line, pa.test))
                    ledger.log(COMPONENT, "FAIL", effect="REJECT", rule_id="pre_authorization", policy_ref=ref,
                               summary=f"'{line.description}' ({fmt_inr(line.amount)}) is a {pa.test}; {reason}",
                               evidence=evidence)

            if rules.requires_pre_auth and not submission.pre_auth_id and line.status == "APPROVED":
                line.reject("PRE_AUTH_MISSING", f"{humanize(cat).capitalize()} claims require pre-authorisation.",
                            f"opd_categories.{cat}.requires_pre_auth")
                pre_auth_missing.append((line, humanize(cat)))

            if line.status == "APPROVED":
                ledger.log(COMPONENT, "PASS", rule_id="line_item",
                           summary=f"Line '{line.description}' ({fmt_inr(line.amount)}) is eligible.",
                           evidence={"line": line.description, "amount": str(line.amount), "file_id": line.source_file_id})

        # Aggregate line findings (one per reason code) for the decision message.
        for code in dict.fromkeys(l.reason_code for l in lines if l.status == "REJECTED"):
            rejected = [l for l in lines if l.reason_code == code]
            listing = "; ".join(f"{l.description} ({fmt_inr(l.amount)}): {l.reason}" for l in rejected)
            member = listing
            if code == "PRE_AUTH_MISSING":
                validity = self.policy.pre_authorization.validity_days
                tests = ", ".join(dict.fromkeys(t for _, t in pre_auth_missing))
                member = (f"{listing} Pre-authorisation must be approved by {self.policy.insurer} BEFORE the {tests}. "
                          f"To resubmit: ask your doctor or the diagnostic centre to raise a pre-authorisation request "
                          f"(with the prescription and clinical notes) through Plum or the insurer; once approved "
                          f"(valid for {validity} days), submit this claim again with the pre-authorisation number in the "
                          f"'Pre-auth ID' field. If the scan was an emergency, contact support for a retrospective review.")
                actions.append(MemberAction(action="OBTAIN_PRE_AUTH",
                                            detail=f"Obtain pre-authorisation for the {tests} and resubmit with the pre-auth ID."))
            findings.append(Finding(code, "REJECT", listing, member, rejected[0].policy_ref))
        for code in dict.fromkeys(l.reason_code for l in lines if l.status == "NEEDS_REVIEW"):
            items = [l for l in lines if l.reason_code == code]
            findings.append(Finding(code, "REVIEW", "; ".join(f"{l.description}: {l.reason}" for l in items),
                                    "Some items on your bill need a medical review by our team.", items[0].policy_ref))

    def _match_named(self, description: str, names: List[str]) -> Optional[str]:
        for name in names:
            if keyword_hits(description, self.interp.aliases_for(name)):
                return name
        return None

    # ================================================================ limits and money

    def _check_per_claim(self, submission, rules: CategoryRules, approved_lines, approved_sum, eligible, ledger,
                         findings, warnings) -> bool:
        cfg = self.interp.per_claim_limit
        global_limit = self.policy.coverage.per_claim_limit
        ceiling = max(global_limit, rules.sub_limit) if cfg.rule == "max_of_global_and_category_sub_limit" else global_limit
        pre_authed = sum((l.amount for l in approved_lines if l.pre_authorized), ZERO)
        base = submission.claimed_amount if cfg.applied_to == "claimed_amount" else eligible
        subject = max(ZERO, base - pre_authed)
        cat = submission.category.lower()
        evidence = {"claimed": str(submission.claimed_amount), "eligible": str(eligible), "pre_authorised": str(pre_authed),
                    "compared_amount": str(subject), "ceiling": str(ceiling), "global_per_claim_limit": str(global_limit),
                    "category_sub_limit": str(rules.sub_limit), "rule": cfg.rule}
        if subject > ceiling:
            source = "per-claim limit" if ceiling == global_limit else f"{humanize(cat)} sub-limit"
            findings.append(self._reject(
                ledger, "per_claim_limit", "PER_CLAIM_EXCEEDED",
                "coverage.per_claim_limit" if ceiling == global_limit else f"opd_categories.{cat}.sub_limit",
                f"The claimed amount of {fmt_inr(submission.claimed_amount)}"
                + (f" (eligible {fmt_inr(subject)})" if subject != submission.claimed_amount else "")
                + f" exceeds the "
                f"{source} of {fmt_inr(ceiling)} for a single {humanize(cat)} claim.",
                evidence=evidence, interpretation_ref="per_claim_limit"))
            return True
        ledger.log(COMPONENT, "PASS", rule_id="per_claim_limit", policy_ref="coverage.per_claim_limit",
                   interpretation_ref="per_claim_limit",
                   summary=f"Eligible amount {fmt_inr(subject)} is within the per-claim ceiling of {fmt_inr(ceiling)}.",
                   evidence=evidence)
        if rules.sub_limit < global_limit and subject > rules.sub_limit:
            note = (f"Eligible amount {fmt_inr(subject)} is above the {humanize(cat)} sub_limit of {fmt_inr(rules.sub_limit)}; "
                    f"treated as advisory per the documented interpretation (see interpretation.assumptions.sub_limit).")
            ledger.log(COMPONENT, "WARN", rule_id="category_sub_limit", policy_ref=f"opd_categories.{cat}.sub_limit",
                       interpretation_ref="assumptions.sub_limit", summary=note, evidence=evidence)
            warnings.append(note)
        return False

    def _waterfall(self, submission, context: MemberContext, rules: CategoryRules, lines: List[_Line], approved_sum,
                   eligible, usable, ledger, findings) -> FinancialBreakdown:
        cat = submission.category.lower()
        steps: List[str] = []
        approved = [l for l in lines if l.status == "APPROVED"]
        billed = sum((l.amount for l in lines), ZERO)
        scale = (eligible / approved_sum) if approved_sum else Decimal(1)
        if eligible < approved_sum:
            steps.append(f"Eligible line items total {fmt_inr(approved_sum)}; capped at the claimed amount "
                         f"{fmt_inr(submission.claimed_amount)}.")
            ledger.log(COMPONENT, "INFO", effect="ADJUST_AMOUNT", rule_id="claimed_amount_cap",
                       summary=f"Eligible items ({fmt_inr(approved_sum)}) exceed the amount claimed; payout is capped at "
                               f"the claimed {fmt_inr(submission.claimed_amount)}.",
                       amount_before=q2(approved_sum), amount_after=q2(eligible))
        else:
            steps.append(f"Eligible amount: {fmt_inr(eligible)} (of {fmt_inr(billed)} billed).")

        # Network discount
        network = self._network_hospital(submission, usable)
        discount_pct = rules.network_discount_percent if network else ZERO
        ref_nd = f"opd_categories.{cat}.network_discount_percent"
        if network and discount_pct > 0:
            ledger.log(COMPONENT, "PASS", effect="ADJUST_AMOUNT", rule_id="network_discount", policy_ref=ref_nd,
                       summary=f"{network} is a network hospital: {discount_pct}% network discount applied first.",
                       evidence={"network_hospital": network, "percent": str(discount_pct)})
        elif network:
            ledger.log(COMPONENT, "INFO", rule_id="network_discount", policy_ref=ref_nd,
                       summary=f"{network} is a network hospital, but {humanize(cat)} has no network discount.")
        else:
            ledger.log(COMPONENT, "INFO", rule_id="network_discount", policy_ref="network_hospitals",
                       summary=f"Provider '{self._provider_name(submission, usable) or 'unknown'}' is not a network hospital; "
                               f"no network discount.")

        base_copay = rules.copay_percent
        branded_pct = rules.branded_drug_copay_percent
        after_discount_total = ZERO
        copay_total = ZERO
        after_copay_total = ZERO
        unknown_brand = []
        for line in approved:
            amount = q2(line.amount * scale)
            discounted = q2(amount - amount * discount_pct / HUNDRED)
            pct = base_copay
            if branded_pct is not None and line.is_branded:
                pct = branded_pct
            elif branded_pct is not None and line.is_branded is None and not line.synthetic:
                unknown_brand.append(line.description)
            copay = q2(discounted * pct / HUNDRED)
            after_discount_total += discounted
            copay_total += copay
            after_copay_total += discounted - copay
        if discount_pct > 0:
            steps.append(f"Network discount {discount_pct}% ({network}): {fmt_inr(q2(eligible))} - "
                         f"{fmt_inr(q2(eligible) - after_discount_total)} = {fmt_inr(after_discount_total)}.")
            ledger.log(COMPONENT, "INFO", effect="ADJUST_AMOUNT", rule_id="network_discount_amount", policy_ref=ref_nd,
                       summary=f"Network discount: {fmt_inr(q2(eligible))} -> {fmt_inr(after_discount_total)}.",
                       amount_before=q2(eligible), amount_after=after_discount_total)
        ref_cp = f"opd_categories.{cat}.copay_percent"
        if copay_total > 0:
            pct_text = f"{base_copay}%" if not any(l.is_branded for l in approved) else \
                f"{base_copay}% generic / {branded_pct}% branded"
            steps.append(f"Co-pay {pct_text} on {fmt_inr(after_discount_total)}: -{fmt_inr(copay_total)} = "
                         f"{fmt_inr(after_copay_total)}.")
            ledger.log(COMPONENT, "PASS", effect="ADJUST_AMOUNT", rule_id="copay", policy_ref=ref_cp,
                       summary=f"Co-pay {pct_text} applied after the network discount: {fmt_inr(copay_total)} deducted "
                               f"({fmt_inr(after_discount_total)} -> {fmt_inr(after_copay_total)}).",
                       amount_before=after_discount_total, amount_after=after_copay_total,
                       evidence={"copay_percent": str(base_copay), "branded_percent": str(branded_pct) if branded_pct else None})
        else:
            ledger.log(COMPONENT, "PASS", rule_id="copay", policy_ref=ref_cp,
                       summary=f"No co-pay applies to {humanize(cat)} ({base_copay}%).")
        if unknown_brand:
            ledger.log(COMPONENT, "WARN", rule_id="branded_drug_copay", policy_ref=f"opd_categories.{cat}.branded_drug_copay_percent",
                       interpretation_ref="assumptions.branded_drugs",
                       summary=f"Brand status unknown for {', '.join(unknown_brand)}; generic co-pay ({base_copay}%) applied.")

        # Annual OPD limit (per patient) and family floater
        amount = after_copay_total
        annual_remaining, family_remaining = self._limits_remaining(submission, context, ledger)
        limit_capped = False
        for label, remaining, ref in (("annual OPD limit", annual_remaining, "coverage.annual_opd_limit"),
                                      ("family floater limit", family_remaining, "coverage.family_floater.combined_limit")):
            if remaining is None:
                continue
            if remaining <= 0:
                findings.append(self._reject(
                    ledger, "annual_limit", "ANNUAL_LIMIT_EXHAUSTED", ref,
                    f"The {label} for this policy year has been fully used.", evidence={"remaining": str(remaining)}))
                amount = ZERO
                break
            if amount > remaining:
                ledger.log(COMPONENT, "WARN", effect="ADJUST_AMOUNT", rule_id="annual_limit", policy_ref=ref,
                           summary=f"Payout {fmt_inr(amount)} exceeds the remaining {label} ({fmt_inr(remaining)}); capped.",
                           amount_before=amount, amount_after=q2(remaining))
                steps.append(f"Capped at remaining {label}: {fmt_inr(remaining)}.")
                amount = q2(remaining)
                limit_capped = True
                findings.append(Finding("ANNUAL_LIMIT_EXHAUSTED", "REJECT",
                                        f"Payout capped at the remaining {label} ({fmt_inr(remaining)}).",
                                        f"Only {fmt_inr(remaining)} of your {label} was left for this policy year, so the "
                                        f"payout is limited to that.", ref))
            else:
                ledger.log(COMPONENT, "PASS", rule_id="annual_limit", policy_ref=ref,
                           summary=f"Payout {fmt_inr(amount)} is within the remaining {label} ({fmt_inr(remaining)}).")

        steps.append(f"Final payable: {fmt_inr(amount)}.")
        return FinancialBreakdown(
            claimed_amount=submission.claimed_amount, billed_amount=q2(billed), eligible_amount=q2(eligible),
            network_hospital=network, network_discount_applied=bool(network and discount_pct > 0),
            network_discount_percent=discount_pct, network_discount_amount=q2(q2(eligible) - after_discount_total),
            after_network_discount=after_discount_total, copay_percent=base_copay, copay_amount=copay_total,
            after_copay=after_copay_total, annual_limit_remaining=annual_remaining,
            family_limit_remaining=family_remaining, final_amount=q2(amount), steps=steps,
        )

    def _limits_remaining(self, submission, context: MemberContext, ledger) -> Tuple[Optional[Decimal], Optional[Decimal]]:
        holder = self.policy.policy_holder
        start, end = holder.policy_start_date, holder.policy_end_date
        caller_ytd = submission.ytd_claims_amount or ZERO
        stored_patient = self.usage([context.patient_id], start, end, submission.claim_id) if self.usage else ZERO
        patient_ytd = max(caller_ytd, stored_patient)
        annual = self.policy.coverage.annual_opd_limit
        annual_remaining = q2(annual - patient_ytd)
        ledger.log(COMPONENT, "INFO", rule_id="ytd_usage", interpretation_ref="assumptions.annual_limits",
                   summary=f"Year-to-date OPD usage for {context.patient_id}: {fmt_inr(patient_ytd)} (caller-supplied "
                           f"{fmt_inr(caller_ytd)}, recorded in this system {fmt_inr(stored_patient)}; the larger is used). "
                           f"Remaining annual OPD limit: {fmt_inr(annual_remaining)} of {fmt_inr(annual)}.",
                   evidence={"caller_ytd": str(caller_ytd), "stored_ytd": str(stored_patient), "annual_limit": str(annual)})
        floater = self.policy.coverage.family_floater
        family_remaining = None
        if floater.enabled and floater.combined_limit is not None:
            stored_family = self.usage(context.family_ids, start, end, submission.claim_id) if self.usage else ZERO
            family_ytd = max(caller_ytd, stored_family)
            family_remaining = q2(floater.combined_limit - family_ytd)
        return annual_remaining, family_remaining

    def _provider_name(self, submission, usable) -> Optional[str]:
        for e in usable:
            if e.bill_data and e.bill_data.hospital_name:
                return e.bill_data.hospital_name
        return submission.hospital_name

    def _network_hospital(self, submission, usable) -> Optional[str]:
        names = [e.bill_data.hospital_name for e in usable if e.bill_data and e.bill_data.hospital_name]
        names = names or ([submission.hospital_name] if submission.hospital_name else [])
        for provider in names:
            tokens = set(re.sub(r"[^a-z0-9\s]", " ", normalize_text(provider)).split())
            for hospital in self.policy.network_hospitals:
                brand = [t for t in re.sub(r"[^a-z0-9\s]", " ", normalize_text(hospital)).split()
                         if t not in _GENERIC_PROVIDER_WORDS]
                if brand and all(t in tokens for t in brand):
                    return hospital
        return None

    # ================================================================ plumbing

    def _reject(self, ledger, rule_id, code, policy_ref, text, evidence=None, interpretation_ref=None,
                confidence_factor=None) -> Finding:
        ledger.log(COMPONENT, "FAIL", effect="REJECT", rule_id=rule_id, policy_ref=policy_ref,
                   interpretation_ref=interpretation_ref, summary=text, evidence={"reason_code": code, **(evidence or {})},
                   confidence_factor=confidence_factor, confidence_scope="ALL")
        return Finding(code, "REJECT", text, text, policy_ref)

    def _finish(self, ledger, watch, submission, findings, lines: List[_Line], financial, warnings, actions) -> PolicyResult:
        rejects = [f for f in findings if f.kind == "REJECT"]
        reviews = [f for f in findings if f.kind == "REVIEW"]
        approved_lines = [l for l in lines if l.status == "APPROVED"]
        rejected_lines = [l for l in lines if l.status == "REJECTED"]
        amount = financial.final_amount if financial else ZERO
        capped = any(f.code == "ANNUAL_LIMIT_EXHAUSTED" for f in rejects)

        if financial is None or amount <= 0:
            proposal = "REJECTED" if (rejects or rejected_lines) else ("MANUAL_REVIEW" if reviews else "REJECTED")
            amount = ZERO
        elif rejected_lines or capped:
            proposal = "PARTIAL"
        else:
            proposal = "APPROVED"
        if reviews and proposal != "REJECTED":
            proposal = "MANUAL_REVIEW"

        ledger.log(COMPONENT, "INFO", rule_id="policy_summary",
                   summary=f"Policy evaluation: {proposal}, payable {fmt_inr(amount)}"
                           + (f"; rejection reasons {', '.join(dict.fromkeys(f.code for f in rejects))}" if rejects else "")
                           + (f"; review reasons {', '.join(dict.fromkeys(f.code for f in reviews))}" if reviews else "")
                           + f". {len(approved_lines)} line(s) approved, {len(rejected_lines)} rejected.",
                   evidence={"proposal": proposal, "approved_amount": str(amount)}, duration_ms=watch.ms())
        return PolicyResult(proposal=proposal, approved_amount=amount, findings=findings,
                            line_items=[l.to_model() for l in lines], financial=financial, warnings=warnings,
                            actions=actions)
