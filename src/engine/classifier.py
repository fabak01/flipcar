"""Deal classification and loan recommendation."""

import logging
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

CONFIG_DIR = Path(__file__).parent.parent.parent / "config"


def _load_params() -> dict[str, Any]:
    """Load classifier parameters."""
    with open(CONFIG_DIR / "params.yaml") as f:
        return yaml.safe_load(f)


def classify_deal(
    profit_result: dict[str, Any],
    comp_result: dict[str, Any],
    listing: dict[str, Any],
    params: dict[str, Any] | None = None,
    soh_analysis: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Classify a deal based on profit scenarios.

    Uses the 80% loan scenario by default. Biased towards showing deals.

    Args:
        profit_result: Output from profit.calculate_profit().
        comp_result: Output from comps.find_comps().
        listing: Normalized listing dict.
        params: Optional params override.

    Returns:
        Dict with classification, flags, and loan_recommendation.
    """
    if params is None:
        params = _load_params()

    clf_params = params["classifier"]

    scenario_80 = profit_result.get("scenarios", {}).get("80pct_loan", {})
    profit_base = scenario_80.get("profit_base", 0)
    profit_bear = scenario_80.get("profit_bear", 0)

    # Classification
    if profit_bear < clf_params["hard_pass_bear"]:
        classification = "HARD PASS"
    elif profit_base > clf_params["green_profit_base"] and profit_bear > clf_params["green_profit_bear"]:
        classification = "KONTAKT"
    elif profit_base > clf_params["yellow_profit_base"] and profit_bear > clf_params["yellow_profit_bear"]:
        classification = "KONTAKT (forsiktig)"
    elif profit_base > clf_params["orange_profit_base"] and profit_bear > clf_params["orange_profit_bear"]:
        classification = "MANUELL VURDERING"
    elif profit_base > clf_params["white_profit_base"] and profit_bear > clf_params["white_profit_bear"]:
        classification = "MONITOR"
    else:
        classification = "PASS"

    # Flags
    flags: list[str] = []
    comp_tier = comp_result.get("tier")
    n_comps = comp_result.get("n_comps", 0)

    if comp_tier == 3 or n_comps < 5:
        flags.append("TYNT COMP-GRUNNLAG")
    if listing.get("dq_score", 1.0) < 0.70:
        flags.append("LAV DATAKVALITET")
    if "INSUFFICIENT_COMPS" in comp_result.get("flags", []):
        flags.append("FOR FA COMPS")

    # Loan recommendation
    if profit_bear > 10000:
        loan_rec = "Hoy laan OK (80%)"
    elif profit_bear > 0:
        loan_rec = "Moderat laan (60%)"
    elif profit_bear > -10000:
        loan_rec = "Lav laan (40%) eller cash"
    else:
        loan_rec = "Ikke bruk laan"

    return {
        "classification": classification,
        "flags": flags,
        "loan_recommendation": loan_rec,
        "profit_base": profit_base,
        "profit_bear": profit_bear,
    }
