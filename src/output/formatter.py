"""Output formatting: JSONL audit trail and CSV action-oriented deals."""

import csv
import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def write_jsonl(records: list[dict[str, Any]], filepath: str = "deals.jsonl") -> None:
    """Write full audit records to JSONL."""
    path = Path(filepath)
    with open(path, "w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    logger.info("Wrote %d records to %s", len(records), path)


def write_csv(records: list[dict[str, Any]], filepath: str = "deals.csv") -> None:
    """Write action-oriented CSV sorted by spread_ask_pct descending."""
    if not records:
        return

    sorted_records = sorted(
        records,
        key=lambda r: r.get("spread_ask_pct", 0),
        reverse=True,
    )

    columns = [
        "listing_id", "url", "make", "model", "year", "mileage_km",
        "asking_price", "market_anchor_price", "market_anchor_low", "market_anchor_high",
        "adjusted_market_value",
        "spread_ask_abs", "spread_ask_pct",
        "realistic_bid_price", "spread_bid_abs", "spread_bid_pct",
        "adj_positive", "adj_negative", "repair_buffer",
        "classification_label", "execution_gate",
        "hard_red_flag", "ai_status", "ai_summary_short",
        "ai_positive_signals", "ai_negative_signals", "ai_missing_info",
        "seller_motivation_score", "soh_status", "eu_status",
        "skip_reason", "explanation",
    ]

    path = Path(filepath)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()

        for r in sorted_records:
            # Truncate explanation for CSV (first line only)
            expl = r.get("explanation", "") or ""
            expl_short = expl.split("\n")[0] if expl else ""

            # Format list fields as semicolon-separated strings
            def _fmt_list(val: Any) -> str:
                if not val:
                    return ""
                if isinstance(val, list):
                    parts = []
                    for item in val[:5]:
                        if isinstance(item, dict):
                            parts.append(item.get("name", item.get("question", str(item))))
                        else:
                            parts.append(str(item))
                    return "; ".join(parts)
                return str(val)

            writer.writerow({
                "listing_id": r.get("listing_id"),
                "url": r.get("listing_url") or r.get("url"),
                "make": r.get("make"),
                "model": r.get("model"),
                "year": r.get("year"),
                "mileage_km": r.get("km") or r.get("mileage_km"),
                "asking_price": r.get("asking_price") or r.get("listing_price_nok"),
                "market_anchor_price": r.get("market_anchor_price"),
                "market_anchor_low": r.get("market_anchor_low"),
                "market_anchor_high": r.get("market_anchor_high"),
                "adjusted_market_value": r.get("adjusted_market_value"),
                "spread_ask_abs": r.get("spread_ask_abs"),
                "spread_ask_pct": r.get("spread_ask_pct"),
                "realistic_bid_price": r.get("realistic_bid_price"),
                "spread_bid_abs": r.get("spread_bid_abs"),
                "spread_bid_pct": r.get("spread_bid_pct"),
                "adj_positive": r.get("adj_positive"),
                "adj_negative": r.get("adj_negative"),
                "repair_buffer": r.get("repair_buffer"),
                "classification_label": r.get("classification_label") or r.get("classification", {}).get("label", ""),
                "execution_gate": r.get("execution_gate") or r.get("classification", {}).get("execution_gate", ""),
                "hard_red_flag": r.get("hard_red_flag"),
                "ai_status": r.get("ai_status"),
                "ai_summary_short": r.get("ai_summary_short"),
                "ai_positive_signals": _fmt_list(r.get("ai_positive_signals")),
                "ai_negative_signals": _fmt_list(r.get("ai_negative_signals")),
                "ai_missing_info": _fmt_list(r.get("ai_missing_info")),
                "seller_motivation_score": r.get("seller_motivation_score"),
                "soh_status": r.get("soh_status"),
                "eu_status": r.get("eu_status"),
                "skip_reason": r.get("skip_reason"),
                "explanation": expl_short,
            })

    logger.info("Wrote %d records to %s", len(sorted_records), path)


# LEGACY: kept for backward compatibility with old tests
def build_audit_record(*args, **kwargs) -> dict[str, Any]:
    """Legacy audit record builder — not used in production."""
    return {}
