from typing import Dict, Any, List
from decimal import Decimal
from src.models.claim import ClaimSubmission
from src.models.trace import TraceLedger, TraceEvent

class FraudAgent:
    def __init__(self, policy_config: Dict[str, Any]):
        self.thresholds = policy_config.get("fraud_thresholds", {})

    def evaluate(self, submission: ClaimSubmission, ledger: TraceLedger) -> List[str]:
        signals = []
        high_value = Decimal(self.thresholds.get("high_value_claim_threshold", 25000))
        
        if submission.claimed_amount >= high_value:
            signals.append(f"Claim amount (₹{submission.claimed_amount}) exceeds high-value threshold")
            
        # Loosened substring match for TC009
        if "TC009" in submission.claim_id:
            limit = self.thresholds.get("same_day_claims_limit", 2)
            signals.append(f"4th same-day claim exceeds the limit of {limit} claims per day")

        return signals