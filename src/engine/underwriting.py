"""Full deal underwriting: comps-based FMV + Pristips market data + AI condition analysis."""

from typing import Any

from .carry import calculate_carry


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

    seller_mod = -0.02 if listing.get("seller_type") == "forhandler" else 0.0

    total_discount = max(base_discount + age_bonus + cut_bonus + seller_mod, 0.01)
    total_discount = min(total_discount, 0.20)

    assumed_entry = round(listing_price * (1 - total_discount))

    return {
        "listing_price": listing_price,
        "assumed_entry_price": assumed_entry,
        "total_discount": round(total_discount, 3),
    }


def underwrite_deal(listing: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
    """Full underwriting of a deal.

    Uses comps-based FMV as market anchor, Pristips for days-to-sell
    and market liquidity, AI analysis for condition adjustments.
    """
    pristips = listing.get("pristips") or {}
    ai = listing.get("ai_analysis") or {}
    comp_result = listing.get("comp_result") or {}

    # === MARKET ANCHOR (from comps) ===
    market_anchor = comp_result.get("transaction_median")
    market_low = None
    market_high = None
    fmv_source = "internal_comps"

    if market_anchor and comp_result.get("comp_transaction_prices"):
        import numpy as np
        prices = np.array(comp_result["comp_transaction_prices"])
        market_low = round(float(np.percentile(prices, 20)))
        market_high = round(float(np.percentile(prices, 80)))
    elif market_anchor:
        market_low = round(market_anchor * 0.90)
        market_high = round(market_anchor * 1.10)

    if market_anchor is None:
        return {
            "listing": listing,
            "market": {"source": "none", "anchor": None},
            "classification": {
                "emoji": "⚪",
                "label": "MONITOR",
                "send_telegram": False,
                "reason": "Ingen markedsdata",
                "loan_rec": "Ikke bruk laan",
            },
            "error": "Ingen markedsdata tilgjengelig",
        }

    # === CONDITION ADJUSTMENTS (from AI) ===
    positive_adj = sum(p.get("value_nok", 0) for p in ai.get("positives", []))
    negative_adj_p50 = sum(i.get("cost_p50", 0) for i in ai.get("issues", []))
    negative_adj_p90 = sum(i.get("cost_p90", 0) for i in ai.get("issues", []))

    # === UNDERWRITTEN EXIT ===
    exit_base = market_anchor + positive_adj - negative_adj_p50
    exit_bear = (market_low or round(market_anchor * 0.9)) + positive_adj * 0.5 - negative_adj_p90
    exit_bull = (market_high or round(market_anchor * 1.1)) + positive_adj - negative_adj_p50 * 0.5

    # === SALES COSTS ===
    sales_fixed = params.get("profit", {}).get("sales_fixed_costs", 1390)
    exit_base -= sales_fixed
    exit_bear -= sales_fixed
    exit_bull -= sales_fixed

    # === ENTRY ===
    entry = estimate_entry_price(listing, pristips, params)
    purchase_price = entry["assumed_entry_price"]

    # === REP (from rep_estimator, already on listing) ===
    rep = listing.get("rep_estimate", {"total_p50": 0, "total_p90": 0})
    rep_p50 = rep.get("total_p50", 0)
    rep_p90 = rep.get("total_p90", 0)

    # === DAYS TO SELL ===
    market_days = pristips.get("days_to_sell")
    if market_days:
        days_base = int(market_days)
    else:
        from .days_to_sell import get_model_baseline
        days_base = get_model_baseline(listing.get("make", ""), listing.get("model", ""), params)

    # Adjust for pricing relative to comps
    if market_anchor and listing.get("price_nok") and listing["price_nok"] < market_anchor * 0.95:
        days_base = round(days_base * 0.7)

    days_bear = round(days_base * 2.0)
    days_bull = round(days_base * 0.6)

    # === FEES ===
    profit_params = params.get("profit", {})
    omreg = profit_params.get("omregistrering", 5500)
    forsikring = (days_base / 30) * profit_params.get("forsikring_per_month", 1500)
    finn_annonse = profit_params.get("finn_salgsannonse", 590)
    total_fees = omreg + forsikring + finn_annonse

    # === CARRY (3 LTV scenarios x 3 time scenarios) ===
    scenarios: dict[str, dict[str, Any]] = {}
    for ltv_label, ltv in [("cash", 0.0), ("60pct", 0.6), ("80pct", 0.8)]:
        carry_base = calculate_carry(purchase_price, days_base, ltv, params)
        carry_bear = calculate_carry(purchase_price, days_bear, ltv, params)
        carry_bull = calculate_carry(purchase_price, days_bull, ltv, params)

        profit_base = exit_base - purchase_price - rep_p50 - carry_base["total_carry"] - total_fees
        profit_bear = exit_bear - purchase_price - rep_p90 - carry_bear["total_carry"] - total_fees
        profit_bull = exit_bull - purchase_price - rep_p50 * 0.5 - carry_bull["total_carry"] - total_fees

        equity = purchase_price * (1 - ltv) if ltv < 1 else purchase_price
        roe_base = (profit_base / equity) * (365 / max(days_base, 1)) if equity > 0 else 0

        scenarios[ltv_label] = {
            "profit_bull": round(profit_bull),
            "profit_base": round(profit_base),
            "profit_bear": round(profit_bear),
            "roe_base_annual": round(roe_base * 100),
            "carry": round(carry_base["total_carry"]),
            "equity_required": round(equity),
        }

    # === MPP ===
    target_profit = params.get("mpp", {}).get("target_profit", 20000)
    max_loss = params.get("mpp", {}).get("max_loss_bear", -10000)

    carry_mpp_base = calculate_carry(purchase_price, days_base, 0.8, params)["total_carry"]
    carry_mpp_bear = calculate_carry(purchase_price, days_bear, 0.8, params)["total_carry"]

    mpp_base = exit_base - rep_p50 - carry_mpp_base - total_fees - target_profit
    mpp_bear = exit_bear - rep_p90 - carry_mpp_bear - total_fees - max_loss
    mpp = round(min(mpp_base, mpp_bear))
    required_discount = 1 - (mpp / listing["price_nok"]) if listing.get("price_nok") and listing["price_nok"] > 0 else 0

    # === SOH ===
    from .battery_soh import calculate_soh_sensitivity
    profit_base_80 = scenarios["80pct"]["profit_base"]
    is_ev = listing.get("fuel_type", "").lower() in ["el", "elektrisk", "electric", "plugin_hybrid", "plug-in hybrid"]
    soh_reported = ai.get("condition_summary", {}).get("batteri_soh") if ai else None
    soh = calculate_soh_sensitivity(
        profit_base_80, is_ev, soh_reported,
        listing.get("make", ""), listing.get("model", ""), int(listing.get("year") or 2026),
    )

    # === CLASSIFY ===
    from .classifier import classify_deal_new
    profit_bear_80 = scenarios["80pct"]["profit_bear"]
    classification = classify_deal_new(profit_base_80, profit_bear_80, listing, ai, soh, pristips)

    return {
        "listing": listing,
        "market": {
            "source": fmv_source,
            "regnr_source": listing.get("regnr_source", "listing"),
            "anchor": market_anchor,
            "low": market_low,
            "high": market_high,
            "days_to_sell": market_days,
            "active_similar": pristips.get("active_similar"),
            "sold_90d": pristips.get("sold_90d"),
            "sold_last_30d": pristips.get("sold_last_30d"),
        },
        "ai_analysis": ai,
        "adjustments": {
            "positive": positive_adj,
            "negative_p50": negative_adj_p50,
            "negative_p90": negative_adj_p90,
        },
        "exit": {
            "base": round(exit_base),
            "bear": round(exit_bear),
            "bull": round(exit_bull),
        },
        "entry": entry,
        "rep": {"p50": rep_p50, "p90": rep_p90},
        "days": {"base": days_base, "bear": days_bear, "bull": days_bull},
        "fees": round(total_fees),
        "scenarios": scenarios,
        "mpp": mpp,
        "required_discount": round(required_discount, 3),
        "soh": soh,
        "classification": classification,
        "comps": {
            "tier": comp_result.get("tier"),
            "n_comps": comp_result.get("n_comps", 0),
        },
    }


# Keep backward-compatible function for existing tests
def calculate_underwritten_exit(
    pristips: dict | None,
    ai_analysis: dict,
    internal_comps: dict | None,
    listing: dict,
    params: dict,
) -> dict[str, Any]:
    """Legacy wrapper - calculate underwritten exit values."""
    if internal_comps and internal_comps.get("transaction_median"):
        anchor_base = internal_comps["transaction_median"]
        anchor_low = internal_comps.get("median_price", anchor_base) * 0.9
        anchor_high = internal_comps.get("median_price", anchor_base) * 1.1
    else:
        return {"error": "Ingen markedsdata tilgjengelig"}

    positive_adj = sum(p.get("value_nok", 0) for p in ai_analysis.get("positives", []))
    negative_adj = sum(i.get("cost_p50", 0) for i in ai_analysis.get("issues", []))
    negative_adj_p90 = sum(i.get("cost_p90", 0) for i in ai_analysis.get("issues", []))

    exit_base = anchor_base + positive_adj - negative_adj
    exit_bear = anchor_low + positive_adj * 0.5 - negative_adj_p90 * 1.3
    exit_bull = anchor_high + positive_adj - negative_adj * 0.5

    sales_fixed = params.get("profit", {}).get("sales_fixed_costs", 1390)
    exit_base -= sales_fixed
    exit_bear -= sales_fixed
    exit_bull -= sales_fixed

    return {
        "market_anchor_price": anchor_base,
        "market_anchor_low": anchor_low,
        "market_anchor_high": anchor_high,
        "fmv_source": "internal_comps",
        "positive_adjustments": positive_adj,
        "negative_adjustments_p50": negative_adj,
        "negative_adjustments_p90": negative_adj_p90,
        "execution_premium": 0,
        "underwritten_exit_base": round(exit_base),
        "underwritten_exit_bear": round(exit_bear),
        "underwritten_exit_bull": round(exit_bull),
        "condition_details": {
            "positives": ai_analysis.get("positives", []),
            "issues": ai_analysis.get("issues", []),
            "diligence": ai_analysis.get("diligence_items", []),
        },
    }
