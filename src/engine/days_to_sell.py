"""Estimated days to sell calculation."""

import logging
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

CONFIG_DIR = Path(__file__).parent.parent.parent / "config"


def _load_params() -> dict[str, Any]:
    """Load days-to-sell parameters."""
    with open(CONFIG_DIR / "params.yaml") as f:
        return yaml.safe_load(f)


def _model_key(make: str, model: str) -> str:
    """Generate config key from make+model."""
    return f"{make}_{model}".lower().replace(" ", "_").replace(".", "").replace("-", "")


def price_factor(price: float, fmv_p50: float) -> float:
    """Calculate pricing factor based on price vs FMV ratio.

    Args:
        price: Listing price.
        fmv_p50: Median fair market value.

    Returns:
        Multiplier for days-to-sell baseline.
    """
    if fmv_p50 <= 0:
        return 1.0
    ratio = price / fmv_p50
    if ratio < 0.85:
        return 0.50
    if ratio < 0.95:
        return 0.75
    if ratio < 1.00:
        return 0.90
    if ratio < 1.05:
        return 1.00
    if ratio < 1.10:
        return 1.40
    if ratio < 1.15:
        return 1.80
    return 2.50


def season_factor(month: int) -> float:
    """Calculate seasonal adjustment factor.

    Args:
        month: Current month (1-12).

    Returns:
        Multiplier for days-to-sell baseline.
    """
    if month in (1, 2, 3):
        return 1.20
    if month in (4, 5, 6):
        return 0.80
    if month == 7:
        return 1.30
    return 1.00


def seller_factor(seller_type: str) -> float:
    """Calculate seller type adjustment.

    Args:
        seller_type: 'privat' or 'forhandler'.

    Returns:
        Multiplier for days-to-sell baseline.
    """
    if seller_type == "forhandler":
        return 0.85
    return 1.00


def estimate_days(
    listing: dict[str, Any],
    fmv_adjusted_p50: float,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Estimate days to sell for a listing.

    Args:
        listing: Normalized listing dict.
        fmv_adjusted_p50: Adjusted FMV median.
        params: Optional params override.

    Returns:
        Dict with p50, p90, and bull estimates.
    """
    if params is None:
        params = _load_params()

    days_params = params["days_to_sell"]
    baselines = days_params["model_baselines"]

    make = listing.get("make", "")
    model = listing.get("model", "")
    key = _model_key(make, model)
    baseline = baselines.get(key, baselines.get("default_ev", 28))

    price = listing.get("price_nok", 0)
    pf = price_factor(price, fmv_adjusted_p50)
    month = datetime.now().month
    sf = season_factor(month)
    sel_f = seller_factor(listing.get("seller_type", "privat"))

    days_p50 = baseline * pf * sf * sel_f
    days_p90 = days_p50 * days_params.get("p90_multiplier", 2.2)
    days_bull = days_p50 * days_params.get("bull_multiplier", 0.6)

    return {
        "p50": round(days_p50),
        "p90": round(days_p90),
        "bull": round(days_bull),
        "baseline": baseline,
        "price_factor": round(pf, 2),
        "season_factor": round(sf, 2),
        "seller_factor": round(sel_f, 2),
    }
