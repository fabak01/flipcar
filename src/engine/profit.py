"""LEGACY: Profit calculation across bull/base/bear scenarios and LTV levels.

This module is NOT used in the canonical production path.
Production profit calculation is inlined in underwriting.py underwrite_deal().
Retained for backward compatibility with tests and diagnostics.
"""

from pathlib import Path
from typing import Any

import yaml

from .carry import calculate_carry

CONFIG_DIR = Path(__file__).parent.parent.parent / "config"


def _load_params() -> dict[str, Any]:
    with open(CONFIG_DIR / "params.yaml") as f:
        return yaml.safe_load(f)


def estimate_entry_price(listing: dict[str, Any], pristips: dict | None, params: dict[str, Any]) -> dict[str, Any]:
    """Estimate entry price with listing-age/price-cut/market modifiers."""
    listing_price = listing.get("price_nok", 0)
    profit_params = params.get("profit", {})

    base_discount = profit_params.get("base_negotiation_discount", 0.03)

    age_days = listing.get("listing_age_days", 0)
    if age_days > 60:
        age_bonus = 0.05
    elif age_days > 30:
        age_bonus = 0.03
    elif age_days > 14:
        age_bonus = 0.01
    else:
        age_bonus = 0.0

    n_cuts = listing.get("n_price_cuts", 0)
    cut_bonus = min(n_cuts * 0.02, 0.06)

    if pristips and pristips.get("market_anchor_price"):
        price_vs_market = listing_price / max(pristips["market_anchor_price"], 1)
        if price_vs_market > 1.10:
            market_bonus = 0.04
        elif price_vs_market > 1.05:
            market_bonus = 0.02
        elif price_vs_market < 0.95:
            market_bonus = -0.02
        else:
            market_bonus = 0.0
    else:
        market_bonus = 0.0

    seller_mod = -0.02 if listing.get("seller_type") == "forhandler" else 0.0

    total_discount = max(base_discount + age_bonus + cut_bonus + market_bonus + seller_mod, 0.01)
    total_discount = min(total_discount, 0.20)

    assumed_entry = round(listing_price * (1 - total_discount))

    return {
        "listing_price": listing_price,
        "listing_price_nok": listing_price,
        "assumed_entry_price": assumed_entry,
        "total_discount": round(total_discount, 3),
        "assumed_negotiation_discount": round(total_discount, 3),
        "discount_breakdown": {
            "base": base_discount,
            "listing_age": age_bonus,
            "price_cuts": cut_bonus,
            "market_position": market_bonus,
            "seller_type": seller_mod,
        },
    }


def calculate_profit(
    listing: dict[str, Any],
    fmv_adjusted: dict[str, Any],
    rep: dict[str, Any],
    days: dict[str, Any],
    params: dict[str, Any] | None = None,
    pristips: dict | None = None,
) -> dict[str, Any]:
    """Calculate profit across scenarios and LTV levels."""
    if params is None:
        params = _load_params()

    profit_params = params["profit"]

    entry = estimate_entry_price(listing, pristips, params)
    listing_price_nok = entry["listing_price_nok"]
    assumed_entry_price = entry["assumed_entry_price"]

    exit_base = fmv_adjusted["adjusted_p50"] * (1 - profit_params["sales_friction_base"]) - profit_params["sales_fixed_costs"]
    exit_bear = fmv_adjusted["adjusted_p10"] * (1 - profit_params["sales_friction_bear"]) - profit_params["sales_fixed_costs"]
    exit_bull = fmv_adjusted["adjusted_p90"] * (1 - profit_params["sales_friction_bull"]) - profit_params["sales_fixed_costs"]

    days_p50 = days["p50"]
    omregistrering = profit_params["omregistrering"]
    forsikring = (days_p50 / 30) * profit_params["forsikring_per_month"]
    finn_annonse = profit_params["finn_salgsannonse"]
    total_fees = omregistrering + forsikring + finn_annonse

    rep_p50 = rep["total_p50"]
    rep_p90 = rep["total_p90"]

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
        "listing_price": listing_price_nok,
        "assumed_negotiation_discount": entry["total_discount"],
        "assumed_entry_price": assumed_entry_price,
        "entry_discount_breakdown": entry["discount_breakdown"],
        "exit_base": round(exit_base),
        "exit_bear": round(exit_bear),
        "exit_bull": round(exit_bull),
        "total_fees": round(total_fees),
        "rep_p50": rep_p50,
        "rep_p90": rep_p90,
        "scenarios": scenarios,
    }
