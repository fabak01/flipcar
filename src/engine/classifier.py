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


def classify_deal_new(
    profit_base: float,
    profit_bear: float,
    listing: dict[str, Any],
    ai: dict | None,
    soh: dict | None,
    pristips: dict | None,
) -> dict[str, Any]:
    """Classify a deal based on profit scenarios. New simplified interface.

    Key rule: Never pitch a deal (KONTAKT) without solid market data.
    Requires either Pristips price or comps to underwrite.
    """

    # Production rule: Pristips price REQUIRED for any classification
    has_ai = ai and (ai.get("issues") or ai.get("positives"))
    has_pristips_price = pristips and pristips.get("market_anchor_price")

    if not has_pristips_price:
        return {
            "emoji": "⚪",
            "label": "PRISTIPS_MISSING",
            "send_telegram": False,
            "reason": "Mangler Pristips-pris (paakrevd for underwriting)",
            "loan_rec": "Ikke bruk laan",
        }

    if not has_ai:
        # We have price data but no AI analysis - don't pitch, manual review
        if profit_base > 15000 and profit_bear > -5000:
            cls = {"emoji": "🟠", "label": "MANUELL VURDERING", "send_telegram": False,
                   "reason": "God profitt men mangler AI-analyse"}
        else:
            cls = {"emoji": "⚪", "label": "MONITOR", "send_telegram": False,
                   "reason": "Mangler AI-analyse"}
        cls["loan_rec"] = "Ikke bruk laan"
        return cls

    # Normal classification (has both market data + AI)
    if profit_base > 15000 and profit_bear > -5000:
        cls = {"emoji": "🟢", "label": "KONTAKT", "send_telegram": True}
    elif profit_base > 10000 and profit_bear > -15000:
        cls = {"emoji": "🟡", "label": "KONTAKT (forsiktig)", "send_telegram": True}
    elif profit_base > 5000 and profit_bear > -25000:
        cls = {"emoji": "🟠", "label": "MANUELL VURDERING", "send_telegram": False}
    elif profit_bear < -30000:
        cls = {"emoji": "🚫", "label": "HARD PASS", "send_telegram": False}
    else:
        cls = {"emoji": "🔴", "label": "PASS", "send_telegram": False}

    # Loan recommendation
    if profit_bear > 10000:
        cls["loan_rec"] = "Hoey laan OK (80%)"
    elif profit_bear > 0:
        cls["loan_rec"] = "Moderat laan (60%)"
    elif profit_bear > -10000:
        cls["loan_rec"] = "Lav laan eller cash"
    else:
        cls["loan_rec"] = "Ikke bruk laan"

    return cls


def classify_deal(
    profit_result: dict[str, Any],
    comp_result: dict[str, Any],
    listing: dict[str, Any],
    params: dict[str, Any] | None = None,
    soh_analysis: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Legacy classifier interface for backward compatibility."""
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
