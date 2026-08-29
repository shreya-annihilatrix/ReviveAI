from dataclasses import dataclass

@dataclass
class GateResult:
    approved: bool
    reason: str = ""
