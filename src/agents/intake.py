"""Intake agent: who is claiming, for whom, under which policy.

Component contract
-------------------
Input:  ClaimSubmission, TraceLedger.
Output: IntakeResult
        ok=True  -> context: MemberContext (resolved member, patient, coverage start
                    date, family ids, roster_incomplete flag)
        ok=False -> message + required_actions (NEEDS_MEMBER_ACTION)
Errors: none raised for bad input; every failure is a traced, member-actionable outcome.

Checks, each logged PASS/FAIL:
  policy_match     submission.policy_id (if given) is the loaded policy
  member_lookup    member_id exists on the roster
  patient_link     patient is the member, or a dependent linked to THAT member.
                   v1 accepted any roster id as patient, so DEP001 could be
                   claimed under EMP002.
  patient_record   a linked dependent with no roster record (DEP003-DEP006 in the
                   supplied roster) is not blocked; it is flagged ROSTER_INCOMPLETE
                   and the claim ends in MANUAL_REVIEW, since the member did
                   nothing wrong.
  dates            treatment_date is not in the future, and submission_date is not
                   before treatment_date
Coverage start = patient's own join_date, else the primary member's, else policy start.
v1 skipped waiting periods for every dependent (no join_date -> KeyError -> None).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Callable, List, Optional

from src.core.config import ENGINE_VERSION
from src.core.text import fmt_date, fmt_inr, humanize
from src.models.claim import ClaimSubmission, MemberAction
from src.models.policy import Member, PolicyConfig
from src.models.trace import Stopwatch, TraceLedger

COMPONENT = "INTAKE"


@dataclass
class MemberContext:
    member: Member
    patient: Optional[Member]
    patient_id: str
    patient_name: Optional[str]
    relationship: str
    coverage_start: date
    family_ids: List[str]
    roster_incomplete: bool = False


@dataclass
class IntakeResult:
    ok: bool
    context: Optional[MemberContext] = None
    message: str = ""
    actions: List[MemberAction] = field(default_factory=list)


class IntakeAgent:
    def __init__(self, policy: PolicyConfig, policy_fingerprint: str, today: Callable[[], date] = date.today):
        self.policy = policy
        self.fingerprint = policy_fingerprint
        self.today = today

    def evaluate(self, submission: ClaimSubmission, ledger: TraceLedger) -> IntakeResult:
        watch = Stopwatch()
        ledger.log(
            COMPONENT, "INFO", rule_id="claim_received", summary=f"Claim {submission.claim_id} received: {humanize(submission.category)} claim "
                                       f"for {fmt_inr(submission.claimed_amount)} with {len(submission.documents)} document(s).",
            evidence={
                "claim_id": submission.claim_id, "member_id": submission.member_id, "patient_id": submission.patient_id,
                "category": submission.category, "treatment_date": submission.treatment_date.isoformat(),
                "claimed_amount": str(submission.claimed_amount), "policy_id": self.policy.policy_id,
                "policy_fingerprint": self.fingerprint, "engine_version": ENGINE_VERSION,
            },
        )

        if submission.policy_id and submission.policy_id != self.policy.policy_id:
            return self._fail(ledger, watch, "policy_match",
                              f"This claim references policy '{submission.policy_id}', but member records are held "
                              f"under policy '{self.policy.policy_id}' ({self.policy.policy_name}). Please check the "
                              f"policy ID on your health card and resubmit.",
                              MemberAction(action="CORRECT_DETAILS", detail="Correct the policy ID."),
                              {"submitted": submission.policy_id, "expected": self.policy.policy_id})

        member = self.policy.member(submission.member_id)
        if member is None:
            return self._fail(ledger, watch, "member_lookup",
                              f"Member ID '{submission.member_id}' was not found on policy {self.policy.policy_id}. "
                              f"Please check the member ID on your health card (it looks like EMP001) and resubmit, "
                              f"or contact your HR team if you joined recently.",
                              MemberAction(action="CORRECT_DETAILS", detail="Correct the member ID."),
                              {"member_id": submission.member_id})
        ledger.log(COMPONENT, "PASS", rule_id="member_lookup", summary=f"Member {member.member_id} ({member.name}) found on the roster.",
                   evidence={"member_id": member.member_id, "relationship": member.relationship})

        patient_id = submission.patient_id or member.member_id
        patient = self.policy.member(patient_id)
        roster_incomplete = False
        if patient_id == member.member_id:
            patient, relationship = member, member.relationship
        else:
            linked = patient_id in member.dependents or (patient is not None and patient.primary_member_id == member.member_id)
            if not linked:
                return self._fail(ledger, watch, "patient_link",
                                  f"Patient '{patient_id}' is not registered as a dependent of member "
                                  f"{member.member_id} ({member.name}). Registered dependents: "
                                  f"{', '.join(member.dependents) or 'none'}. Please select the correct patient, or "
                                  f"ask HR to add the dependent to your policy.",
                                  MemberAction(action="CORRECT_DETAILS", detail="Choose a patient covered under your membership."),
                                  {"patient_id": patient_id, "member_dependents": member.dependents})
            if patient is None:
                roster_incomplete = True
                relationship = "UNKNOWN"
                ledger.log(COMPONENT, "WARN", effect="REVIEW", rule_id="patient_record",
                           summary=f"{patient_id} is listed as a dependent of {member.member_id} but has no roster record "
                                   f"(name, relationship, join date unknown); claim will be routed to manual review.",
                           evidence={"patient_id": patient_id})
            else:
                relationship = patient.relationship
        ledger.log(COMPONENT, "PASS" if not roster_incomplete else "WARN", rule_id="patient_link",
                   summary=f"Patient {patient_id} is covered under member {member.member_id} as {relationship}.",
                   evidence={"patient_id": patient_id, "relationship": relationship})

        today = self.today()
        if submission.treatment_date > today:
            return self._fail(ledger, watch, "treatment_date_not_future",
                              f"The treatment date {fmt_date(submission.treatment_date)} is in the future. Please enter "
                              f"the date shown on your bill.",
                              MemberAction(action="CORRECT_DETAILS", detail="Correct the treatment date."),
                              {"treatment_date": submission.treatment_date.isoformat(), "today": today.isoformat()})
        if submission.submission_date and submission.submission_date < submission.treatment_date:
            return self._fail(ledger, watch, "submission_after_treatment",
                              "The submission date is before the treatment date. Please check both dates.",
                              MemberAction(action="CORRECT_DETAILS", detail="Correct the dates."),
                              {"treatment_date": submission.treatment_date.isoformat(),
                               "submission_date": submission.submission_date.isoformat()})
        ledger.log(COMPONENT, "PASS", rule_id="dates", summary="Treatment and submission dates are valid.",
                   evidence={"treatment_date": submission.treatment_date.isoformat(),
                             "submission_date": submission.submission_date.isoformat() if submission.submission_date else None})

        coverage_start, start_source = self._coverage_start(member, patient)
        family = [member.member_id] + list(member.dependents)
        context = MemberContext(
            member=member, patient=patient, patient_id=patient_id,
            patient_name=patient.name if patient else None, relationship=relationship,
            coverage_start=coverage_start, family_ids=family, roster_incomplete=roster_incomplete,
        )
        ledger.log(COMPONENT, "PASS", rule_id="coverage_start",
                   summary=f"Coverage for {patient_id} started {fmt_date(coverage_start)} ({start_source}).",
                   evidence={"coverage_start": coverage_start.isoformat(), "source": start_source},
                   duration_ms=watch.ms())
        return IntakeResult(ok=True, context=context)

    def _coverage_start(self, member: Member, patient: Optional[Member]):
        if patient is not None and patient.join_date:
            return patient.join_date, "patient join_date"
        if member.join_date:
            return member.join_date, "primary member join_date"
        return self.policy.policy_holder.policy_start_date, "policy start date"

    @staticmethod
    def _fail(ledger, watch, rule_id, message, action, evidence) -> IntakeResult:
        ledger.log(COMPONENT, "FAIL", effect="BLOCK", rule_id=rule_id, summary=message, evidence=evidence, duration_ms=watch.ms())
        return IntakeResult(ok=False, message=message, actions=[action])
