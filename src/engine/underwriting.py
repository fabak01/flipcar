"""Lean underwriting: Pristips anchor -> adjustments -> repair buffer -> spread -> action.

Production rule:
- Pristips market_anchor_price REQUIRED for underwriting
- No Pristips -> PASS / NO_PRISTIPS
- Comps kept for diagnostics only
- AI enriches but never blocks classification
"""

import logging
from typing import Any

from .adjustments import detect_adjustments
from .classifier import classify_deal_v2, detect_hard_red_flags
from .spec_pricing import detect_specs, summarize_specs

logger = logging.getLogger(__name__)


def _fmt_nok(n: int | float | None) -> str:
    """Format NOK value with thousand separators."""
    if n is None:
        return "N/A"
    return f"{int(round(n)):,}".replace(",", " ")


def _estimate_realistic_bid(listing: dict[str, Any]) -> dict[str, Any]:
    """Estimate realistic bid price with simple discount logic.

    - Private seller: base 3% discount
    - Dealer: base 1% discount
    - +2% if listing age > 30 days
    - +2% if at least one price cut
    - Cap total at 8%
    """
    listing_price = listing.get("price_nok") or 0
    is_dealer = listing.get("seller_type") == "forhandler"

    base_discount = 0.01 if is_dealer else 0.03

    age_days = listing.get("listing_age_days", 0)
    age_bonus = 0.02 if age_days > 30 else 0.0

    n_cuts = listing.get("n_price_cuts", 0)
    cut_bonus = 0.02 if n_cuts >= 1 else 0.0

    total_discount = min(base_discount + age_bonus + cut_bonus, 0.08)
    realistic_bid = round(listing_price * (1 - total_discount))

    return {
        "listing_price": listing_price,
        "realistic_bid_price": realistic_bid,
        "bid_discount_pct": round(total_discount, 3),
        "breakdown": {
            "base_discount": base_discount,
            "age_bonus": age_bonus,
            "cut_bonus": cut_bonus,
        },
    }


def _compute_confidence(listing: dict[str, Any], ai: dict[str, Any], regnr_source: str) -> str:
    """Compute deal confidence level for execution gating.

    HIGH: own regnr + private seller + AI ok + minimal missing info
    MEDIUM: own regnr + some info missing or dealer with full info
    LOW: reference regnr OR dealer with generic text OR many missing fields
    """
    is_dealer = listing.get("seller_type") == "forhandler"
    is_reference_regnr = regnr_source == "reference"
    missing_info = ai.get("missing_info", [])
    ai_ok = ai.get("ai_status") == "ok"

    if is_reference_regnr:
        return "LOW"

    if is_dealer and (not ai_ok or len(missing_info) >= 3):
        return "LOW"

    if not is_dealer and ai_ok and len(missing_info) <= 1:
        return "HIGH"

    return "MEDIUM"


def _compute_repair_buffer(listing: dict[str, Any]) -> int:
    """Lean repair buffer: text_issue_p50 + 0.5 * expected_model_issue_cost.

    Deeper repair numbers remain in rep_estimate for debug/JSONL output,
    but the main classification uses this simplified buffer.
    """
    rep = listing.get("rep_estimate") or {}
    text_p50 = rep.get("lag1_p50", 0)
    model_expected = rep.get("lag2_expected", 0)
    return round(text_p50 + 0.5 * model_expected)


def _build_explanation(
    label: str, gate: str, reason: str,
    listing_price: int, market_anchor: int, v_adj: int,
    spread_ask_pct: float, spread_bid_pct: float,
    realistic_bid: int, adj_pos: int, adj_neg: int, rep_buffer: int,
    hard_red_flags: list[str], ai_status: str, ai_summary: str,
) -> str:
    """Build a human-readable explanation."""
    lines: list[str] = []
    lines.append(f"Annonsepris: {_fmt_nok(listing_price)} kr")
    lines.append(f"Pristips: {_fmt_nok(market_anchor)} kr")
    lines.append(f"Justert markedsverdi: {_fmt_nok(v_adj)} kr")
    lines.append(f"Spread at ask: {spread_ask_pct:.1%}")
    lines.append(f"Realistisk bud: {_fmt_nok(realistic_bid)} kr (spread: {spread_bid_pct:.1%})")

    if adj_pos > 0:
        lines.append(f"Positive justeringer: +{_fmt_nok(adj_pos)} kr")
    if adj_neg > 0:
        lines.append(f"Negative justeringer: -{_fmt_nok(adj_neg)} kr")
    if rep_buffer > 0:
        lines.append(f"Rep-buffer: -{_fmt_nok(rep_buffer)} kr")

    if hard_red_flags:
        lines.append(f"Hard red flags: {', '.join(hard_red_flags)}")

    lines.append(f"AI: {ai_summary}")
    lines.append(f"")
    lines.append(f"[{label}] {reason}")

    if gate == "SEND_NO_AI":
        lines.append("NB: AI utilgjengelig — klassifisering basert paa regler")

    return "\n".join(lines)


def underwrite_deal(listing: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
    """Lean underwriting: anchor -> adjustments -> spread -> action.

    Production rule: Pristips market_anchor_price is REQUIRED.
    No Pristips -> PASS / NO_PRISTIPS.
    AI enriches but never blocks.
    """
    pristips = listing.get("pristips") or {}
    ai = listing.get("ai_analysis") or {}
    comp_result = listing.get("comp_result") or {}
    ai_status = ai.get("ai_status", "short_text")

    # === MARKET ANCHOR (Pristips ONLY) ===
    market_anchor = pristips.get("market_anchor_price")
    market_low = pristips.get("market_anchor_low")
    market_high = pristips.get("market_anchor_high")

    if market_anchor is None:
        reason = "Pristips-pris mangler — kan ikke underwrite"
        return {
            "listing": listing,
            "classification": {"label": "PASS", "execution_gate": "NO_PRISTIPS", "reason": reason},
            "market": {"source": "none", "anchor": None},
            "explanation": f"PASS: {reason}",
            "ai_status": ai_status,
            "error": reason,
        }

    listing_price = listing.get("price_nok") or 0

    # === ADJUSTMENTS (spec_adjustments.yaml owns equipment, issue_catalog owns condition) ===
    # Catalog adjustments: EU, service, tires, damage, SOH (from issue_catalog.yaml)
    catalog_adjustments = detect_adjustments(listing)
    catalog_positive = sum(a["amount"] for a in catalog_adjustments if a["amount"] > 0)
    catalog_negative = abs(sum(a["amount"] for a in catalog_adjustments if a["amount"] < 0))

    # Spec adjustments: equipment features (from spec_adjustments.yaml — SOLE owner of equipment)
    spec_matches = detect_specs(listing)
    spec_summary = summarize_specs(spec_matches)
    spec_positive = spec_summary["total_positive_nok"]
    spec_negative = abs(spec_summary["total_negative_nok"])

    # AI-extracted adjustments (enrichment only, not gating)
    ai_positive = sum(p.get("value_nok", 0) for p in ai.get("positives", []))
    ai_negative = sum(i.get("cost_p50", 0) for i in ai.get("issues", []))

    # Total adjustments (before cap)
    adj_pos_raw = catalog_positive + spec_positive + ai_positive
    adj_neg = catalog_negative + spec_negative + ai_negative

    # Cap positive adjustments by seller type — dealers write polished text, not polished cars
    is_dealer = listing.get("seller_type") == "forhandler"
    adj_pos_cap = 15_000 if is_dealer else 40_000
    adj_pos = min(adj_pos_raw, adj_pos_cap)
    adj_pos_capped = adj_pos_raw > adj_pos_cap

    # === REPAIR BUFFER (lean: text_p50 + 0.5 * model_expected) ===
    rep_buffer = _compute_repair_buffer(listing)

    # === ADJUSTED MARKET VALUE ===
    v_adj = market_anchor + adj_pos - adj_neg - rep_buffer

    # === SPREADS ===
    spread_ask_abs = v_adj - listing_price
    spread_ask_pct = (v_adj - listing_price) / v_adj if v_adj > 0 else 0.0

    bid_result = _estimate_realistic_bid(listing)
    realistic_bid = bid_result["realistic_bid_price"]
    spread_bid_abs = v_adj - realistic_bid
    spread_bid_pct = (v_adj - realistic_bid) / v_adj if v_adj > 0 else 0.0

    # === CONFIDENCE SCORING ===
    regnr_source = listing.get("regnr_source", "own")
    is_rep_listing = regnr_source == "reference"  # Pristips was looked up via borrowed regnr
    confidence = _compute_confidence(listing, ai, regnr_source)

    # === HIGH TEXT DEPENDENCY FLAG ===
    # If positive adjustments exceed 15% of anchor price, the deal depends too heavily on
    # text-based signals which are unreliable (especially for dealers)
    high_text_dependency = market_anchor > 0 and adj_pos_raw > 0.15 * market_anchor

    # === HARD RED FLAGS ===
    listing_text = listing.get("listing_text", "")
    hard_red_flags = detect_hard_red_flags(listing_text)
    has_hard_red_flag = len(hard_red_flags) > 0

    # Also check AI-reported red flag
    if ai.get("hard_red_flag"):
        has_hard_red_flag = True
        hard_red_flags.append("ai_detected_red_flag")

    # === SOH STATUS ===
    soh_status = "unknown"
    cond = ai.get("condition_summary", {})
    if isinstance(cond, dict):
        soh_val = cond.get("batteri_soh")
        if soh_val and isinstance(soh_val, (int, float)):
            if soh_val >= 90:
                soh_status = "stated_good"
            elif soh_val >= 80:
                soh_status = "stated_ok"
            else:
                soh_status = "stated_bad"
        elif soh_val is None:
            soh_status = "not_mentioned"
    elif isinstance(cond, str):
        soh_status = "unknown"

    # EU status from catalog adjustments
    eu_status = "unknown"
    for adj in catalog_adjustments:
        atype = adj.get("type", "")
        if "eu_kontroll" in atype:
            if "godkjent_fersk" in atype:
                eu_status = "recent"
            elif "godkjent" in atype:
                eu_status = "valid"
            elif "forfalt" in atype:
                eu_status = "expired"
            elif "nær_forfall" in atype or "snart_forfalt" in atype:
                eu_status = "expiring_soon"
            elif "ikke_nevnt" in atype:
                eu_status = "not_mentioned"
            break

    # === CLASSIFY ===
    classification = classify_deal_v2(
        spread_ask_pct=spread_ask_pct,
        spread_bid_pct=spread_bid_pct,
        hard_red_flag=has_hard_red_flag,
        has_pristips=True,
        ai_status=ai_status,
    )

    # Rep listings and LOW confidence deals are never actionable
    if is_rep_listing and classification["execution_gate"] in ("SEND", "SEND_NO_AI"):
        classification = dict(classification)
        classification["execution_gate"] = "BLOCKED"
        classification["reason"] += " [BLOCKED: reference regnr]"
    elif confidence == "LOW" and classification["execution_gate"] in ("SEND", "SEND_NO_AI"):
        classification = dict(classification)
        classification["execution_gate"] = "BLOCKED"
        classification["reason"] += " [BLOCKED: LOW confidence]"

    # AI summary
    ai_summary = ai.get("ai_summary_short", "")
    if not ai_summary:
        ai_summary = "AI unavailable — rule-based only" if ai_status != "ok" else "No summary"

    # === EXPLANATION ===
    explanation = _build_explanation(
        classification["label"], classification["execution_gate"], classification["reason"],
        listing_price, market_anchor, v_adj,
        spread_ask_pct, spread_bid_pct, realistic_bid,
        adj_pos, adj_neg, rep_buffer,
        hard_red_flags, ai_status, ai_summary,
    )

    # === MARKET DAYS ===
    market_days = pristips.get("market_days_to_sell")

    return {
        "listing": listing,
        "classification": classification,
        "market": {
            "source": "finn_pristips",
            "regnr_source": listing.get("regnr_source", "listing"),
            "anchor": market_anchor,
            "low": market_low,
            "high": market_high,
            "days_to_sell": market_days,
            "active_similar": pristips.get("market_active_similar"),
            "sold_90d": pristips.get("market_sold_90d"),
        },
        # Core output
        "asking_price": listing_price,
        "market_anchor_price": market_anchor,
        "adjusted_market_value": v_adj,
        "spread_ask_abs": spread_ask_abs,
        "spread_ask_pct": round(spread_ask_pct, 4),
        "realistic_bid_price": realistic_bid,
        "bid_discount_pct": bid_result["bid_discount_pct"],
        "spread_bid_abs": spread_bid_abs,
        "spread_bid_pct": round(spread_bid_pct, 4),
        "adj_positive": adj_pos,
        "adj_positive_raw": adj_pos_raw,
        "adj_positive_capped": adj_pos_capped,
        "adj_negative": adj_neg,
        "repair_buffer": rep_buffer,
        "confidence": confidence,
        "is_rep_listing": is_rep_listing,
        "high_text_dependency": high_text_dependency,
        "hard_red_flag": has_hard_red_flag,
        "hard_red_flag_details": hard_red_flags,
        # Status fields
        "ai_status": ai_status,
        "ai_summary_short": ai_summary,
        "ai_positive_signals": ai.get("positive_signals", ai.get("positives", [])),
        "ai_negative_signals": ai.get("negative_signals", ai.get("issues", [])),
        "ai_missing_info": ai.get("missing_info", ai.get("diligence_items", [])),
        "seller_motivation_score": ai.get("seller_motivation_score"),
        "soh_status": soh_status,
        "eu_status": eu_status,
        # Detail breakdowns (for JSONL / debug)
        "adjustments_detail": {
            "catalog": catalog_adjustments,
            "catalog_positive": catalog_positive,
            "catalog_negative": catalog_negative,
            "spec_matches": spec_matches,
            "spec_positive": spec_positive,
            "spec_negative": spec_negative,
            "ai_positive": ai_positive,
            "ai_negative": ai_negative,
            "adj_pos_raw": adj_pos_raw,
            "adj_pos_cap": adj_pos_cap,
            "adj_pos_capped": adj_pos_capped,
        },
        "bid_detail": bid_result,
        "rep_detail": listing.get("rep_estimate", {}),
        "comps": {
            "tier": comp_result.get("tier"),
            "n_comps": comp_result.get("n_comps", 0),
        },
        "ai_analysis": ai,
        "skip_reason": listing.get("skip_reason"),
        "explanation": explanation,
    }


# LEGACY: backward-compatible function for old tests only.
def calculate_underwritten_exit(
    pristips: dict | None,
    ai_analysis: dict,
    internal_comps: dict | None,
    listing: dict,
    params: dict,
) -> dict[str, Any]:
    """Legacy wrapper."""
    if internal_comps and internal_comps.get("transaction_median"):
        anchor_base = internal_comps["transaction_median"]
    else:
        return {"error": "Ingen markedsdata tilgjengelig"}

    positive_adj = sum(p.get("value_nok", 0) for p in ai_analysis.get("positives", []))
    negative_adj = sum(i.get("cost_p50", 0) for i in ai_analysis.get("issues", []))
    sales_fixed = params.get("profit", {}).get("sales_fixed_costs", 1390)

    exit_base = anchor_base + positive_adj - negative_adj - sales_fixed

    return {
        "market_anchor_price": anchor_base,
        "fmv_source": "internal_comps",
        "positive_adjustments": positive_adj,
        "negative_adjustments_p50": negative_adj,
        "underwritten_exit_base": round(exit_base),
    }
