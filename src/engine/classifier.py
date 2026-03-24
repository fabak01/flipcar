"""Deal classification: spread-based action labels for practical car flipping.

Labels: CALL_NOW / MESSAGE / WATCH / PASS
Execution gate: SEND / SEND_NO_AI / NO_PRISTIPS / BLOCKED
"""

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# Hard red flag patterns — any match forces PASS
HARD_RED_FLAG_PATTERNS = [
    r"taxi|drosje",
    r"kollisjon|krasj|ulykke(?:s)?(?:bil|skade)",
    r"totalskade|kondemnert",
    r"motor(?:havari|problem|feil)|girkasse(?:problem|feil)",
    r"rust(?:skade|hull|gjennomslag)",
    r"soh\s*(?:under\s*)?[1-6]\d\b",  # SOH below 70
]

# "selges som den er" combined with fault indications
AS_IS_PLUS_FAULT_PATTERNS = [
    (r"selges\s+som\s+den\s+er|as\s+is|uten\s+garanti",
     r"feil|problem|defekt|virker\s+ikke|lekk|rust|skade|varsel"),
]


def detect_hard_red_flags(listing_text: str) -> list[str]:
    """Detect hard red flags from listing text. Returns list of matched flag names."""
    text_lower = listing_text.lower()
    flags: list[str] = []

    for pattern in HARD_RED_FLAG_PATTERNS:
        if re.search(pattern, text_lower):
            flags.append(pattern)

    # "selges som den er" + fault indication = red flag
    for as_is_pat, fault_pat in AS_IS_PLUS_FAULT_PATTERNS:
        if re.search(as_is_pat, text_lower) and re.search(fault_pat, text_lower):
            flags.append("as_is_plus_fault")

    return flags


def classify_deal_v2(
    spread_ask_pct: float,
    spread_bid_pct: float,
    hard_red_flag: bool,
    has_pristips: bool,
    ai_status: str,
) -> dict[str, Any]:
    """Classify a deal using spread-based action labels.

    Args:
        spread_ask_pct: (V_adj - P_list) / V_adj
        spread_bid_pct: (V_adj - realistic_bid) / V_adj
        hard_red_flag: True if any hard red flag detected
        has_pristips: True if valid Pristips anchor exists
        ai_status: one of ok, fallback_*, short_text
    """
    if not has_pristips:
        return {
            "label": "PASS",
            "execution_gate": "NO_PRISTIPS",
            "reason": "Ingen Pristips-pris tilgjengelig",
        }

    if hard_red_flag:
        return {
            "label": "PASS",
            "execution_gate": "BLOCKED",
            "reason": "Hard red flag detected",
        }

    # Spread-based classification
    if spread_ask_pct >= 0.10:
        label = "CALL_NOW"
    elif spread_bid_pct >= 0.06 and spread_ask_pct >= 0.04:
        label = "MESSAGE"
    elif spread_bid_pct >= 0.02:
        label = "WATCH"
    else:
        label = "PASS"

    # Execution gate
    if label in ("CALL_NOW", "MESSAGE"):
        if ai_status == "ok":
            gate = "SEND"
        else:
            gate = "SEND_NO_AI"
    elif label == "WATCH":
        gate = "BLOCKED"
    else:
        gate = "BLOCKED"

    reason = _build_reason(label, spread_ask_pct, spread_bid_pct)

    return {
        "label": label,
        "execution_gate": gate,
        "reason": reason,
    }


def _build_reason(label: str, spread_ask_pct: float, spread_bid_pct: float) -> str:
    """Build human-readable classification reason."""
    ask_pct = f"{spread_ask_pct:.1%}"
    bid_pct = f"{spread_bid_pct:.1%}"

    if label == "CALL_NOW":
        return f"Spread at ask {ask_pct} — strong buy signal"
    elif label == "MESSAGE":
        return f"Spread at bid {bid_pct}, at ask {ask_pct} — worth negotiating"
    elif label == "WATCH":
        return f"Spread at bid {bid_pct} — marginal, monitor for price drop"
    else:
        return f"Spread at bid {bid_pct} — insufficient margin"


# === LEGACY: kept for backward compatibility with old tests ===

def classify_deal_new(
    profit_base: float,
    profit_bear: float,
    listing: dict[str, Any],
    ai: dict | None,
    soh: dict | None,
    pristips: dict | None,
) -> dict[str, Any]:
    """Legacy classifier — maps old profit-based logic to new labels for compatibility."""
    has_pristips_price = pristips and pristips.get("market_anchor_price")

    if not has_pristips_price:
        return {
            "label": "PASS",
            "execution_gate": "NO_PRISTIPS",
            "reason": "Mangler Pristips-pris (paakrevd for underwriting)",
        }

    if profit_base > 15000 and profit_bear > -5000:
        label = "CALL_NOW"
    elif profit_base > 10000 and profit_bear > -15000:
        label = "MESSAGE"
    elif profit_base > 5000 and profit_bear > -25000:
        label = "WATCH"
    else:
        label = "PASS"

    has_ai = ai and (ai.get("issues") or ai.get("positives") or ai.get("positive_signals"))
    gate = "SEND" if has_ai else "SEND_NO_AI"
    if label in ("WATCH", "PASS"):
        gate = "BLOCKED"

    return {
        "label": label,
        "execution_gate": gate,
        "reason": f"profit_base={profit_base:+,.0f}, profit_bear={profit_bear:+,.0f}",
    }


def classify_deal(
    profit_result: dict[str, Any],
    comp_result: dict[str, Any],
    listing: dict[str, Any],
    params: dict[str, Any] | None = None,
    soh_analysis: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Legacy classifier interface — kept for old tests."""
    scenario_80 = profit_result.get("scenarios", {}).get("80pct_loan", {})
    profit_base = scenario_80.get("profit_base", 0)
    profit_bear = scenario_80.get("profit_bear", 0)
    return classify_deal_new(profit_base, profit_bear, listing, None, None, None)
