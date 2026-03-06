"""Days-to-sell estimation with Pristips as primary source."""

from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

CONFIG_DIR = Path(__file__).parent.parent.parent / "config"


def _load_params() -> dict[str, Any]:
    with open(CONFIG_DIR / "params.yaml") as f:
        return yaml.safe_load(f)


def _model_key(make: str, model: str) -> str:
    return f"{make}_{model}".lower().replace(" ", "_").replace(".", "").replace("-", "")


def get_model_baseline(make: str, model: str, params: dict[str, Any]) -> int:
    baselines = params["days_to_sell"]["model_baselines"]
    key = _model_key(make, model)
    return baselines.get(key, baselines.get("default_ice", 22))


def estimate_days_to_sell(listing: dict[str, Any], pristips: dict | None, params: dict[str, Any]) -> dict[str, Any]:
    """Estimate days to sell using Pristips baseline when available."""
    if pristips and pristips.get("market_days_to_sell"):
        baseline = float(pristips["market_days_to_sell"])
        source = "finn_pristips"
    else:
        baseline = float(get_model_baseline(listing.get("make", ""), listing.get("model", ""), params))
        source = "internal_model"

    factor = 1.0

    if pristips and pristips.get("market_anchor_price") and listing.get("price_nok"):
        ratio = listing["price_nok"] / max(float(pristips["market_anchor_price"]), 1)
        if ratio < 0.90:
            factor *= 0.6
        elif ratio < 0.95:
            factor *= 0.8
        elif ratio > 1.10:
            factor *= 1.8
        elif ratio > 1.05:
            factor *= 1.3

    month = datetime.now().month
    if month in [1, 2, 3]:
        factor *= 1.15
    elif month in [4, 5, 6]:
        factor *= 0.85
    elif month == 7:
        factor *= 1.25

    days_p50 = round(baseline * factor)
    days_p90 = round(days_p50 * 2.0)
    days_bull = round(days_p50 * 0.6)

    return {
        "days_p50": days_p50,
        "days_p90": days_p90,
        "days_bull": days_bull,
        "baseline": round(baseline),
        "source": source,
        "adjustment_factor": round(factor, 2),
    }


# Backward-compatible shape used by existing pipeline/tests.
def estimate_days(listing: dict[str, Any], fmv_adjusted_p50: float, params: dict[str, Any] | None = None) -> dict[str, Any]:
    if params is None:
        params = _load_params()
    fake_pristips = {"market_anchor_price": fmv_adjusted_p50}
    out = estimate_days_to_sell(listing, fake_pristips, params)
    return {
        "p50": out["days_p50"],
        "p90": out["days_p90"],
        "bull": out["days_bull"],
        "baseline": out["baseline"],
        "source": out["source"],
        "adjustment_factor": out["adjustment_factor"],
    }


def price_factor(price: float, fmv_p50: float) -> float:
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
    if month in (1, 2, 3):
        return 1.20
    if month in (4, 5, 6):
        return 0.80
    if month == 7:
        return 1.30
    return 1.00
