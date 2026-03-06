"""Comp selection and matching for FMV calculation."""

import logging
from typing import Any

import numpy as np
import yaml
from pathlib import Path

logger = logging.getLogger(__name__)

CONFIG_DIR = Path(__file__).parent.parent.parent / "config"


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

    Args:
        target: The listing to find comps for.
        all_listings: All available listings to search through.
        params: Optional override for comp parameters.

    Returns:
        Dict with tier, comps list, and metadata.
    """
    if params is None:
        params = _load_params()

    comp_params = params["comps"]
    tx_discount = params["transaction_discount"]

    target_id = target.get("listing_id")
    target_make = target.get("make", "")
    target_model = target.get("model", "")
    target_variant = target.get("variant", "unknown")
    target_year = target.get("year")
    target_km = target.get("km")

    if not target_year or not target_km:
        return {
            "tier": None,
            "n_comps": 0,
            "comps": [],
            "comp_ids": [],
            "flags": ["INSUFFICIENT_COMPS"],
        }

    # Filter to same make+model, excluding target itself
    same_model = [
        l for l in all_listings
        if l.get("make") == target_make
        and l.get("model") == target_model
        and l.get("listing_id") != target_id
        and l.get("price_nok")
        and l.get("year")
        and l.get("km")
    ]

    # Try tiers in order
    for tier_name, tier_cfg in [("tier1", comp_params["tier1"]), ("tier2", comp_params["tier2"]), ("tier3", comp_params["tier3"])]:
        tier_num = int(tier_name[-1])
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
            return _build_comp_result(comps, tier_num, tx_discount, comp_params)

    # Insufficient comps - use whatever we have from tier 3
    tier3 = comp_params["tier3"]
    comps = [
        l for l in same_model
        if abs(l["year"] - target_year) <= tier3["year_delta"]
        and abs(l["km"] - target_km) / max(target_km, 1) <= tier3["km_delta_pct"]
    ]

    result = _build_comp_result(comps, 3, tx_discount, comp_params)
    result["flags"] = result.get("flags", []) + ["INSUFFICIENT_COMPS"]
    return result


def _build_comp_result(
    comps: list[dict[str, Any]],
    tier: int,
    tx_discount: dict[str, float],
    comp_params: dict[str, Any],
) -> dict[str, Any]:
    """Build comp result with transaction prices and outlier removal."""
    # Calculate transaction prices
    for comp in comps:
        seller = comp.get("seller_type", "privat")
        discount = tx_discount.get(seller, tx_discount.get("privat", 0.07))
        comp["transaction_price"] = comp["price_nok"] * (1 - discount)

    # Outlier removal
    prices = np.array([c["transaction_price"] for c in comps])
    if len(prices) > 4:
        p_low = np.percentile(prices, comp_params.get("outlier_low_pct", 5))
        p_high = np.percentile(prices, comp_params.get("outlier_high_pct", 95))
        filtered = [c for c in comps if p_low <= c["transaction_price"] <= p_high]
        if len(filtered) >= 3:
            comps = filtered

    tx_prices = [c["transaction_price"] for c in comps]
    median_raw = float(np.median([c["price_nok"] for c in comps])) if comps else 0
    median_tx = float(np.median(tx_prices)) if tx_prices else 0

    return {
        "tier": tier,
        "n_comps": len(comps),
        "comps": comps,
        "comp_ids": [c.get("listing_id", "") for c in comps],
        "comp_transaction_prices": tx_prices,
        "median_price": round(median_raw),
        "transaction_median": round(median_tx),
        "flags": [],
    }
