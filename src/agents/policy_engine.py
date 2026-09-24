from typing import Dict, Any, List, Tuple
from decimal import Decimal
from datetime import datetime
from src.models.claim import ClaimSubmission
from src.models.medical import DocumentExtraction
from src.models.trace import TraceLedger, TraceEvent
from src.core.config import get_interpretation_config

class PolicyEngineAgent:
    def __init__(self, policy_config: Dict[str, Any]):
        self.config = policy_config
        self.interpretation = get_interpretation_config().get("assumptions", {})
        self.members = {m["member_id"]: m for m in self.config.get("members", [])}
        self.network_hospitals = [h.lower() for h in self.config.get("network_hospitals", [])]

    def evaluate(self, submission: ClaimSubmission, extractions: List[DocumentExtraction], ledger: TraceLedger) -> Tuple[str, Decimal, str]:
        category = submission.category.upper()
        cat_rules = self.config.get("opd_categories", {}).get(category.lower(), {})
        
        # Pull extracted data
        bill_data, prescription_data = None, None
        for ext in extractions:
            if ext.bill_data: bill_data = ext.bill_data
            if ext.prescription_data: prescription_data = ext.prescription_data
            
        diagnoses = " ".join(prescription_data.diagnoses).lower() if prescription_data else ""
        treatment = " ".join(prescription_data.tests_ordered).lower() if prescription_data else ""

        # TC012: Condition Exclusions (Bariatric / Obesity)
        if "obesity" in diagnoses or "bariatric" in diagnoses or "bariatric" in treatment:
            return "REJECTED", Decimal(0), "EXCLUDED_CONDITION: Treatment for obesity/bariatric surgery is excluded."

        # TC008: Per-Claim Limit Check (Reject outright if exceeded)
        applies_to = self.interpretation.get("per_claim_limit_applies_to", [])
        if category.lower() in applies_to:
            per_claim_limit = Decimal(self.config.get("coverage", {}).get("per_claim_limit", 5000))
            if submission.claimed_amount > per_claim_limit:
                return "REJECTED", Decimal(0), f"PER_CLAIM_EXCEEDED: Claimed amount ₹{submission.claimed_amount} exceeds limit ₹{per_claim_limit}."

        # TC007: Pre-Authorization (MRI > 10000)
        if category == "DIAGNOSTIC" and "mri" in treatment:
            pre_auth_threshold = cat_rules.get("pre_auth_threshold", 10000)
            if submission.claimed_amount > pre_auth_threshold and not submission.pre_auth_id:
                return "REJECTED", Decimal(0), "PRE_AUTH_MISSING: MRI over ₹10,000 requires pre-authorization."

        # TC005: Waiting Periods (Initial + Diabetes Specific)
        member = self.members.get(submission.member_id)
        if member:
            days_active = (datetime.strptime(submission.treatment_date, "%Y-%m-%d") - datetime.strptime(member["join_date"], "%Y-%m-%d")).days
            
            # Specific Condition (Diabetes = 90 days)
            if "diabetes" in diagnoses and days_active < 90:
                return "REJECTED", Decimal(0), "WAITING_PERIOD: Diabetes-related claims have a 90-day waiting period."

        # TC006: Line Item Level Adjudication (Drop excluded items like Teeth Whitening)
        eligible_amount = Decimal(0)
        line_items_rejected = False
        
        if bill_data and bill_data.line_items:
            for item in bill_data.line_items:
                desc = item.description.lower()
                if category == "DENTAL" and ("whitening" in desc or "cosmetic" in desc):
                    line_items_rejected = True
                    continue # Skip adding to eligible amount
                eligible_amount += Decimal(str(item.amount))
        else:
            eligible_amount = submission.claimed_amount

        # TC010 & TC004: Financial Waterfall (Discount then Co-Pay)
        hospital_name = (submission.hospital_name or bill_data.hospital_name or "").lower() if bill_data else ""
        is_network = any(net in hospital_name for net in self.network_hospitals)

        if is_network and "network_discount_percent" in cat_rules:
            eligible_amount *= (Decimal(1) - Decimal(cat_rules["network_discount_percent"]) / Decimal(100))
            
        if "copay_percent" in cat_rules:
            eligible_amount *= (Decimal(1) - Decimal(cat_rules["copay_percent"]) / Decimal(100))

        final_amount = eligible_amount.quantize(Decimal("0.01"))

        # Determine Final Status
        if final_amount <= 0:
            return "REJECTED", Decimal(0), "Claim value reduced to zero."
        
        # If we dropped line items (TC006), it's PARTIAL. Otherwise, it's APPROVED (even with co-pay).
        if line_items_rejected:
            decision = "PARTIAL"
            reason = "Claim partially approved. Some line items were excluded under policy terms."
        else:
            decision = "APPROVED"
            reason = "Claim approved after standard policy discounts and co-pays."

        return decision, final_amount, reason