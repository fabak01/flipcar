"""Profit calculation across bull/base/bear scenarios and LTV levels."""

import logging
from pathlib import Path
from typing import Any

import yaml

from .carry import calculate_carry

logger = logging.getLogger(__name__)

CONFIG_DIR = Path(__file__).parent.parent.parent / "config"


def _load_params() -> dict[str, Any]:
    """Load profit parameters."""
    with open(CONFIG_DIR / "params.yaml") as f:
        return yaml.safe_load(f)


def calculate_profit(
    listing: dict[str, Any],
    fmv_adjusted: dict[str, Any],
    rep: dict[str, Any],
    days: dict[str, Any],
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Calculate profit across all scenarios and LTV levels.

    Args:
        listing: Normalized listing dict.
        fmv_adjusted: Dict with adjusted_p10/p50/p90.
        rep: Dict with total_p50/total_p90.
        days: Dict with p50/p90/bull days estimates.
        params: Optional params override.

    Returns:
        Dict with scenarios per LTV, entry/exit prices, and fees.
    """
    if params is None:
        params = _load_params()

    profit_params = params["profit"]

    # Entry
    listing_price_nok = listing.get("price_nok", 0)
    assumed_negotiation_discount = profit_params["negotiation_discount"]
    assumed_entry_price = listing_price_nok * (1 - assumed_negotiation_discount)

    # Exit prices
    exit_base = (
        fmv_adjusted["adjusted_p50"] * (1 - profit_params["sales_friction_base"])
        - profit_params["sales_fixed_costs"]
    )
    exit_bear = (
        fmv_adjusted["adjusted_p10"] * (1 - profit_params["sales_friction_bear"])
        - profit_params["sales_fixed_costs"]
    )
    exit_bull = (
        fmv_adjusted["adjusted_p90"] * (1 - profit_params["sales_friction_bull"])
        - profit_params["sales_fixed_costs"]
    )

    # Fees
    days_p50 = days["p50"]
    omregistrering = profit_params["omregistrering"]
    forsikring = (days_p50 / 30) * profit_params["forsikring_per_month"]
    finn_annonse = profit_params["finn_salgsannonse"]
    total_fees = omregistrering + forsikring + finn_annonse

    # Rep costs
    rep_p50 = rep["total_p50"]
    rep_p90 = rep["total_p90"]

    # Calculate for each LTV
    ltv_scenarios = {"cash": 0.0, "60pct_loan": 0.6, "80pct_loan": 0.8}
    scenarios: dict[str, dict[str, Any]] = {}

    for label, ltv in ltv_scenarios.items():
        carry_base = calculate_carry(assumed_entry_price, days_p50, ltv, params)
        carry_bear = calculate_carry(assumed_entry_price, days["p90"], ltv, params)
        carry_bull = calculate_carry(assumed_entry_price, days["bull"], ltv, params)

        profit_base = exit_base - assumed_entry_price - rep_p50 - carry_base["total_carry"] - total_fees
        profit_bear = exit_bear - assumed_entry_price - rep_p90 - carry_bear["total_carry"] - total_fees
        profit_bull = exit_bull - assumed_entry_price - rep_p50 * 0.5 - carry_bull["total_carry"] - total_fees

        equity = carry_base["equity_required"]
        roe_base = (profit_base / equity) * (365 / days_p50) if equity > 0 and days_p50 > 0 else None
        roe_bear = (profit_bear / equity) * (365 / days["p90"]) if equity > 0 and days["p90"] > 0 else None

        scenarios[label] = {
            "profit_base": round(profit_base),
            "profit_bear": round(profit_bear),
            "profit_bull": round(profit_bull),
            "roe_base": f"{roe_base:.0%}" if roe_base is not None else None,
            "roe_bear": f"{roe_bear:.0%}" if roe_bear is not None else None,
            "carry_base": carry_base["total_carry"],
            "carry_bear": carry_bear["total_carry"],
            "equity_required": equity,
        }

    return {
        "listing_price_nok": listing_price_nok,
        "assumed_negotiation_discount": assumed_negotiation_discount,
        "assumed_entry_price": round(assumed_entry_price),
        "exit_base": round(exit_base),
        "exit_bear": round(exit_bear),
        "exit_bull": round(exit_bull),
        "total_fees": round(total_fees),
        "rep_p50": rep_p50,
        "rep_p90": rep_p90,
        "scenarios": scenarios,
    }
