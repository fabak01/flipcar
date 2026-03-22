"""LEGACY: FMV (Fair Market Value) calculation from comp data.

This module is NOT used in the canonical production path.
Production uses Pristips as the sole market anchor (see underwriting.py).
Retained for backward compatibility with tests and diagnostics.
"""

import logging
from typing import Any

import numpy as np
import yaml
from pathlib import Path

logger = logging.getLogger(__name__)

CONFIG_DIR = Path(__file__).parent.parent.parent / "config"


def _load_params() -> dict[str, Any]:
    """Load FMV parameters from config."""
    with open(CONFIG_DIR / "params.yaml") as f:
        return yaml.safe_load(f)


def calculate_fmv(
    comp_result: dict[str, Any],
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Calculate Fair Market Value from comp transaction prices.

    Args:
        comp_result: Output from comps.find_comps().
        params: Optional override for FMV parameters.

    Returns:
        Dict with raw_p10, raw_p50, raw_p90 and metadata.
    """
    if params is None:
        params = _load_params()

    fmv_params = params["fmv"]
    tx_prices = comp_result.get("comp_transaction_prices", [])
    n_comps = len(tx_prices)

    if n_comps == 0:
        return {
            "raw_p10": 0,
            "raw_p50": 0,
            "raw_p90": 0,
            "n_comps": 0,
            "flags": ["NO_COMPS"],
        }

    prices = np.array(tx_prices)
    p10 = float(np.percentile(prices, 10))
    p50 = float(np.percentile(prices, 50))
    p90 = float(np.percentile(prices, 90))

    # Uncertainty expansion for low comp count
    low_n_threshold = fmv_params.get("low_n_threshold", 15)
    expansion_per_missing = fmv_params.get("expansion_per_missing", 0.05)

    if n_comps < low_n_threshold:
        expansion_factor = 1 + (low_n_threshold - n_comps) * expansion_per_missing
        midpoint = p50
        p10 = midpoint - (midpoint - p10) * expansion_factor
        p90 = midpoint + (p90 - midpoint) * expansion_factor
        logger.debug(
            "Expanded FMV range by factor %.2f (n_comps=%d)", expansion_factor, n_comps
        )

    return {
        "raw_p10": round(p10),
        "raw_p50": round(p50),
        "raw_p90": round(p90),
        "n_comps": n_comps,
        "flags": [],
    }
