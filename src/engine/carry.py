"""Capital cost (carry) calculation for different LTV scenarios."""

import logging
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

CONFIG_DIR = Path(__file__).parent.parent.parent / "config"


def _load_params() -> dict[str, Any]:
    """Load carry parameters."""
    with open(CONFIG_DIR / "params.yaml") as f:
        return yaml.safe_load(f)


def calculate_carry(
    purchase_price: float,
    days: float,
    ltv: float,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Calculate capital cost for a given LTV scenario.

    Args:
        purchase_price: Total purchase price in NOK.
        days: Estimated holding period in days.
        ltv: Loan-to-value ratio (0.0, 0.6, or 0.8).
        params: Optional params override.

    Returns:
        Dict with loan_cost, opportunity_cost, total_carry, equity_required.
    """
    if params is None:
        params = _load_params()

    carry_params = params["carry"]
    interest_rate = carry_params["interest_rate"]
    establishment_fee = carry_params["establishment_fee"]
    target_return = carry_params["target_return_equity"]

    loan = purchase_price * ltv
    equity = purchase_price * (1 - ltv)

    loan_cost = loan * (interest_rate / 365) * days
    if ltv > 0:
        loan_cost += establishment_fee

    opp_cost = equity * (target_return / 365) * days

    return {
        "loan_cost": round(loan_cost),
        "opportunity_cost": round(opp_cost),
        "total_carry": round(loan_cost + opp_cost),
        "equity_required": round(equity),
        "ltv": ltv,
        "days": round(days),
    }


def calculate_all_scenarios(
    purchase_price: float,
    days_p50: float,
    days_p90: float,
    days_bull: float,
    params: dict[str, Any] | None = None,
) -> dict[str, dict[str, dict[str, Any]]]:
    """Calculate carry for all LTV and time scenarios.

    Args:
        purchase_price: Total purchase price in NOK.
        days_p50: Base case days.
        days_p90: Bear case days.
        days_bull: Bull case days.
        params: Optional params override.

    Returns:
        Nested dict: {ltv_label: {scenario: carry_result}}.
    """
    ltv_scenarios = {"cash": 0.0, "60pct_loan": 0.6, "80pct_loan": 0.8}
    results: dict[str, dict[str, dict[str, Any]]] = {}

    for label, ltv in ltv_scenarios.items():
        results[label] = {
            "base": calculate_carry(purchase_price, days_p50, ltv, params),
            "bear": calculate_carry(purchase_price, days_p90, ltv, params),
            "bull": calculate_carry(purchase_price, days_bull, ltv, params),
        }

    return results
