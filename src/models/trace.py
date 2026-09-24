from pydantic import BaseModel, Field, ConfigDict
from typing import List, Optional, Dict, Any, Literal
from datetime import datetime
from decimal import Decimal

class TraceEvent(BaseModel):
    # Forbid extra fields so our agents don't hallucinate random JSON keys
    model_config = ConfigDict(extra="forbid")
    
    seq: int
    timestamp: datetime = Field(default_factory=datetime.utcnow)
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
    events: List[TraceEvent] = Field(default_factory=list)
    
    def append(self, event: TraceEvent):
        self.events.append(event)
        
    def get_degraded_components(self) -> List[str]:
        # Scans the ledger to find any component that threw an error
        return list(set(e.component for e in self.events if e.outcome == "ERROR"))