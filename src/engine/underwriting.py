"""Underwritten exit calculations using Pristips as primary market anchor."""

from typing import Any


def calculate_underwritten_exit(
    pristips: dict | None,
    ai_analysis: dict,
    internal_comps: dict | None,
    listing: dict,
    params: dict,
) -> dict[str, Any]:
    """Calculate underwritten exit in bull/base/bear scenarios."""

    if pristips and pristips.get("market_anchor_price"):
        anchor_base = pristips["market_anchor_price"]
        anchor_low = pristips.get("market_anchor_low") or pristips["market_anchor_price"] * 0.9
        anchor_high = pristips.get("market_anchor_high") or pristips["market_anchor_price"] * 1.1
        fmv_source = "finn_pristips"
    elif internal_comps and internal_comps.get("transaction_median"):
        anchor_base = internal_comps["transaction_median"]
        anchor_low = internal_comps.get("median_price", anchor_base) * 0.9
        anchor_high = internal_comps.get("median_price", anchor_base) * 1.1
        fmv_source = "internal_comps"
    else:
        return {"error": "Ingen markedsdata tilgjengelig"}

    positive_adj = sum(p.get("value_nok", 0) for p in ai_analysis.get("positives", []))
    negative_adj = sum(i.get("cost_p50", 0) for i in ai_analysis.get("issues", []))
    negative_adj_p90 = sum(i.get("cost_p90", 0) for i in ai_analysis.get("issues", []))

    execution_premium = 0

    exit_base = anchor_base + positive_adj - negative_adj + execution_premium
    exit_bear = anchor_low + positive_adj * 0.5 - negative_adj_p90 * 1.3
    exit_bull = anchor_high + positive_adj - negative_adj * 0.5 + execution_premium

    sales_fixed = params.get("profit", {}).get("sales_fixed_costs", 1390)
    exit_base -= sales_fixed
    exit_bear -= sales_fixed
    exit_bull -= sales_fixed

    return {
        "market_anchor_price": anchor_base,
        "market_anchor_low": anchor_low,
        "market_anchor_high": anchor_high,
        "fmv_source": fmv_source,
        "positive_adjustments": positive_adj,
        "negative_adjustments_p50": negative_adj,
        "negative_adjustments_p90": negative_adj_p90,
        "execution_premium": execution_premium,
        "underwritten_exit_base": round(exit_base),
        "underwritten_exit_bear": round(exit_bear),
        "underwritten_exit_bull": round(exit_bull),
        "condition_details": {
            "positives": ai_analysis.get("positives", []),
            "issues": ai_analysis.get("issues", []),
            "diligence": ai_analysis.get("diligence_items", []),
        },
    }
