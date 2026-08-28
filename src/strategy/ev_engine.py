"""
Expected Value (EV) Engine — Phase 6.

Calculates the net expected value in ₹ for any proposed action.
Takes into account the margin revenue and the cost of the action.
"""

import argparse
from dataclasses import dataclass
from typing import Any, Callable

ACTION_COSTS: dict[str, float | Callable[[float], float]] = {
    "retry":                   0.50,   # ₹ — processing cost
    "payment_link":            0.15,   # ₹ — SMS/link cost
    "sms_reminder":            0.15,
    "whatsapp":                0.35,
    "discount_10pct":          lambda amt: amt * 0.10,  # margin cost
    "human_escalation":       40.00,   # ₹ — human time
    "split_payment":           0.30,
    "do_nothing":              0.00,
}

@dataclass
class DummyTxn:
    """A minimal mock transaction for EV calculations."""
    amount: float
    margin_rate: float


def calculate_ev(action_type: str, txn: Any, recovery_probability: float) -> float:
    """
    Calculate Expected Value (EV) of an action.
    EV = (Amount * Margin * Recovery_Probability) - Cost
    """
    revenue = txn.amount * txn.margin_rate * recovery_probability
    
    cost_val = ACTION_COSTS.get(action_type, 0.0)
    if callable(cost_val):
        cost = cost_val(txn.amount)
    else:
        cost = cost_val
        
    return revenue - cost

def rank_actions(txn: Any, probabilities: dict[str, float]) -> list[tuple[str, float]]:
    """
    Given a mapping of action_type -> estimated recovery_probability,
    return a list of (action, EV) sorted by EV descending.
    If the max EV is < 0, do_nothing should win (assuming do_nothing has EV=0).
    """
    results = []
    
    # Ensure do_nothing is always evaluated
    if "do_nothing" not in probabilities:
        probabilities["do_nothing"] = 0.0  # Assumes 0% recovery if we do nothing
        
    for action, prob in probabilities.items():
        ev = calculate_ev(action, txn, prob)
        results.append((action, ev))
        
    # Sort descending
    results.sort(key=lambda x: x[1], reverse=True)
    
    # If all EV < 0, do_nothing is chosen
    if results[0][1] < 0:
        return [("do_nothing", 0.0)]
        
    return results

# ---------------------------------------------------------------------------
# CLI Demo
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--demo", action="store_true", help="Run EV engine demo")
    args = parser.parse_args()
    
    if args.demo:
        # Sample transaction: ₹5000, 15% margin
        txn = DummyTxn(amount=5000.0, margin_rate=0.15)
        
        # Hypothetical recovery probabilities estimated by the agent/model
        probabilities = {
            "retry": 0.20,             # 20%
            "payment_link": 0.35,      # 35%
            "whatsapp": 0.40,          # 40%
            "discount_10pct": 0.55,    # 55%
            "human_escalation": 0.60,  # 60%
            "do_nothing": 0.05,        # 5%
        }
        
        print("="*60)
        print(f"EV ENGINE DEMO")
        print(f"Transaction: Amount=Rs.{txn.amount:.2f}, Margin={txn.margin_rate:.0%}")
        print("="*60)
        
        ranked = rank_actions(txn, probabilities)
        
        print(f"{'Rank':<5} | {'Action':<20} | {'Prob':<6} | {'EV (Rs.)':>10}")
        print("-" * 60)
        
        for i, (action, ev) in enumerate(ranked):
            prob = probabilities.get(action, 0.0)
            print(f"{i+1:<5} | {action:<20} | {prob:>5.0%} | {ev:>10.2f}")
            
        print("="*60)
        
        best_action = ranked[0][0]
        print(f"Selected Action: {best_action.upper()} (Max EV: Rs.{ranked[0][1]:.2f})")
