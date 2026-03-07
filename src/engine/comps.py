"""Comp selection and matching for FMV calculation (fallback when Pristips unavailable)."""

import logging
from typing import Any

import numpy as np
import yaml
from pathlib import Path

logger = logging.getLogger(__name__)

CONFIG_DIR = Path(__file__).parent.parent.parent / "config"

# Stricter tier config
TIER_CONFIG = {
    1: {"year_delta": 1, "km_delta_pct": 0.20, "min_count": 8},
    2: {"year_delta": 1, "km_delta_pct": 0.30, "min_count": 8},
    3: {"year_delta": 2, "km_delta_pct": 0.40, "min_count": 5},
}

MAX_KM_DIFF_ABSOLUTE = 50000
MAX_PRICE_RATIO = 2.5
MIN_PRICE_RATIO = 0.4


def _load_params() -> dict[str, Any]:
    """Load comp parameters from config."""
    with open(CONFIG_DIR / "params.yaml") as f:
        return yaml.safe_load(f)


def find_comps(
    target: dict[str, Any],
    all_listings: list[dict[str, Any]],
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Find comparable listings for a target listing.

    Uses stricter tiers. Returns insufficient=True if < 5 comps.
    """
    if params is None:
        params = _load_params()

    tx_discount = params["transaction_discount"]

    target_id = target.get("listing_id")
    target_make = target.get("make", "")
    target_model = target.get("model", "")
    target_variant = target.get("variant", "unknown")
    target_year = target.get("year")
    target_km = target.get("km")
    target_price = target.get("price_nok")

    if not target_year or not target_km:
        return {
            "tier": None,
            "n_comps": 0,
            "comps": [],
            "comp_ids": [],
            "comp_transaction_prices": [],
            "median_price": None,
            "transaction_median": None,
            "flags": ["INSUFFICIENT_COMPS"],
            "insufficient": True,
        }

    # Filter to same make+model, excluding target itself
    same_model = []
    for l in all_listings:
        if l.get("make") != target_make or l.get("model") != target_model:
            continue
        if l.get("listing_id") == target_id:
            continue
        if not l.get("price_nok") or not l.get("year") or not l.get("km"):
            continue
        # Price sanity: exclude extreme outliers
        if target_price and l["price_nok"] > 0:
            ratio = l["price_nok"] / target_price
            if ratio > MAX_PRICE_RATIO or ratio < MIN_PRICE_RATIO:
                continue
        # Absolute km difference cap
        if abs(l["km"] - target_km) > MAX_KM_DIFF_ABSOLUTE:
            continue
        same_model.append(l)

    # Try tiers in order
    for tier_num in [1, 2, 3]:
        tier_cfg = TIER_CONFIG[tier_num]
        year_delta = tier_cfg["year_delta"]
        km_delta_pct = tier_cfg["km_delta_pct"]
        min_count = tier_cfg["min_count"]

        comps = []
        for l in same_model:
            if abs(l["year"] - target_year) > year_delta:
                continue
            km_diff = abs(l["km"] - target_km) / max(target_km, 1)
            if km_diff > km_delta_pct:
                continue
            # Tier 1 also requires variant match
            if tier_num == 1 and target_variant != "unknown":
                if l.get("variant", "unknown") != target_variant:
                    continue
            comps.append(l)

        if len(comps) >= min_count:
            return _build_comp_result(comps, tier_num, tx_discount)

    # Insufficient comps - use whatever we have from tier 3
    tier3 = TIER_CONFIG[3]
    comps = [
        l for l in same_model
        if abs(l["year"] - target_year) <= tier3["year_delta"]
        and abs(l["km"] - target_km) / max(target_km, 1) <= tier3["km_delta_pct"]
    ]

    if len(comps) < 5:
        result = _build_comp_result(comps, 3, tx_discount)
        result["flags"] = result.get("flags", []) + ["INSUFFICIENT_COMPS"]
        result["insufficient"] = True
        return result

    result = _build_comp_result(comps, 3, tx_discount)
    result["flags"] = result.get("flags", []) + ["INSUFFICIENT_COMPS"]
    return result


def _build_comp_result(
    comps: list[dict[str, Any]],
    tier: int,
    tx_discount: dict[str, float],
) -> dict[str, Any]:
    """Build comp result with transaction prices and outlier removal."""
    if not comps:
        return {
            "tier": tier,
            "n_comps": 0,
            "comps": [],
            "comp_ids": [],
            "comp_transaction_prices": [],
            "median_price": None,
            "transaction_median": None,
            "flags": ["INSUFFICIENT_COMPS"],
            "insufficient": True,
        }

    # Calculate transaction prices
    for comp in comps:
        seller = comp.get("seller_type", "privat")
        discount = tx_discount.get(seller, tx_discount.get("privat", 0.07))
        comp["transaction_price"] = comp["price_nok"] * (1 - discount)

    # Outlier removal (5th-95th percentile)
    prices = np.array([c["transaction_price"] for c in comps])
    if len(prices) > 4:
        p_low = np.percentile(prices, 5)
        p_high = np.percentile(prices, 95)
        filtered = [c for c in comps if p_low <= c["transaction_price"] <= p_high]
        if len(filtered) >= 3:
            comps = filtered

    tx_prices = [c["transaction_price"] for c in comps]
    median_raw = float(np.median([c["price_nok"] for c in comps])) if comps else 0
    median_tx = float(np.median(tx_prices)) if tx_prices else 0
    insufficient = len(comps) < 5

    return {
        "tier": tier,
        "n_comps": len(comps),
        "comps": comps,
        "comp_ids": [c.get("listing_id", "") for c in comps],
        "comp_transaction_prices": tx_prices,
        "median_price": round(median_raw),
        "transaction_median": round(median_tx),
        "flags": [],
        "insufficient": insufficient,
    }
