"""LEGACY: MPP (Makspris / Maximum Purchase Price) calculation.

This module is NOT used in the canonical production path.
Production MPP is computed inline in underwriting.py underwrite_deal().
Retained for backward compatibility with tests and diagnostics.
"""

import logging
from pathlib import Path
from typing import Any

import yaml

from .carry import calculate_carry

logger = logging.getLogger(__name__)

CONFIG_DIR = Path(__file__).parent.parent.parent / "config"


def _load_params() -> dict[str, Any]:
    """Load MPP parameters."""
    with open(CONFIG_DIR / "params.yaml") as f:
        return yaml.safe_load(f)


def calculate_mpp(
    fmv_adjusted: dict[str, Any],
    rep: dict[str, Any],
    days: dict[str, Any],
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Calculate maximum purchase price (walk-away price).

    Uses the default LTV (80%) and both base and bear scenarios,
    then takes the more conservative (lower) MPP.

    Args:
        fmv_adjusted: Dict with adjusted_p10/p50/p90.
        rep: Dict with total_p50/total_p90.
        days: Dict with p50/p90 days estimates.
        params: Optional params override.

    Returns:
        Dict with mpp, mpp_base, and mpp_bear.
    """
    if params is None:
        params = _load_params()

    mpp_params = params["mpp"]
    profit_params = params["profit"]

    target_profit = mpp_params["target_profit"]
    max_loss_bear = mpp_params["max_loss_bear"]
    default_ltv = mpp_params["default_ltv"]

    # Exit prices
    exit_base = (
        fmv_adjusted["adjusted_p50"] * (1 - profit_params["sales_friction_base"])
        - profit_params["sales_fixed_costs"]
    )
    exit_bear = (
        fmv_adjusted["adjusted_p10"] * (1 - profit_params["sales_friction_bear"])
        - profit_params["sales_fixed_costs"]
    )

    # Fees (using p50 days for base, p90 for bear)
    days_p50 = days["p50"]
    days_p90 = days["p90"]
    omregistrering = profit_params["omregistrering"]
    forsikring_base = (days_p50 / 30) * profit_params["forsikring_per_month"]
    forsikring_bear = (days_p90 / 30) * profit_params["forsikring_per_month"]
    finn_annonse = profit_params["finn_salgsannonse"]
    fees_base = omregistrering + forsikring_base + finn_annonse
    fees_bear = omregistrering + forsikring_bear + finn_annonse

    # Carry estimate (approximate, using exit_base as proxy for purchase price)
    # We solve iteratively: MPP = exit - rep - carry(MPP) - fees - target
    # Simplified: use FMV_p50 as purchase price proxy for carry
    proxy_price = fmv_adjusted["adjusted_p50"]
    carry_base = calculate_carry(proxy_price, days_p50, default_ltv, params)
    carry_bear = calculate_carry(proxy_price, days_p90, default_ltv, params)

    mpp_base = exit_base - rep["total_p50"] - carry_base["total_carry"] - fees_base - target_profit
    mpp_bear = exit_bear - rep["total_p90"] - carry_bear["total_carry"] - fees_bear - max_loss_bear

    mpp = min(mpp_base, mpp_bear)

    return {
        "mpp": round(max(mpp, 0)),
        "mpp_base": round(mpp_base),
        "mpp_bear": round(mpp_bear),
    }
