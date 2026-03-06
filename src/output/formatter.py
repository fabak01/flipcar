"""Output formatting: JSONL audit trail and CSV top-deals."""

import csv
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def build_audit_record(
    listing: dict[str, Any],
    comp_result: dict[str, Any],
    fmv_raw: dict[str, Any],
    fmv_adjusted: dict[str, Any],
    rep: dict[str, Any],
    days: dict[str, Any],
    profit_result: dict[str, Any],
    mpp_data: dict[str, Any],
    classification: dict[str, Any],
) -> dict[str, Any]:
    """Build a full audit trail record for a single listing.

    Args:
        listing: Normalized listing dict.
        comp_result: Comps output.
        fmv_raw: Raw FMV values.
        fmv_adjusted: Adjusted FMV values with adjustments list.
        rep: Repair estimate.
        days: Days-to-sell estimate.
        profit_result: Profit calculation result.
        mpp_data: MPP calculation result.
        classification: Classification result.

    Returns:
        Complete audit dict ready for JSON serialization.
    """
    listing_price = listing.get("price_nok", 0)
    mpp_val = mpp_data.get("mpp", 0)
    required_discount = f"{(1 - mpp_val / listing_price):.1%}" if listing_price > 0 else "N/A"

    return {
        "listing_id": listing.get("listing_id"),
        "listing_url": listing.get("listing_url"),
        "make": listing.get("make"),
        "model": listing.get("model"),
        "variant": listing.get("variant"),
        "year": listing.get("year"),
        "km": listing.get("km"),
        "price_nok": listing_price,
        "location": listing.get("location_city"),
        "dq_score": listing.get("dq_score"),
        "comps": {
            "tier": comp_result.get("tier"),
            "n_comps": comp_result.get("n_comps"),
            "comp_ids": comp_result.get("comp_ids", []),
            "median_price": comp_result.get("median_price"),
            "transaction_median": comp_result.get("transaction_median"),
        },
        "fmv": {
            "raw_p10": fmv_raw.get("raw_p10"),
            "raw_p50": fmv_raw.get("raw_p50"),
            "raw_p90": fmv_raw.get("raw_p90"),
            "adjustments": fmv_adjusted.get("adjustments", []),
            "adjusted_p10": fmv_adjusted.get("adjusted_p10"),
            "adjusted_p50": fmv_adjusted.get("adjusted_p50"),
            "adjusted_p90": fmv_adjusted.get("adjusted_p90"),
        },
        "rep": {
            "lag1_issues": rep.get("lag1_issues", []),
            "lag2_issues": [
                {"name": i["name"], "prob": i["probability"], "cost_p50": i["cost_p50"], "confidence": i["confidence"]}
                for i in rep.get("lag2_issues", [])
            ],
            "correlation_factor": rep.get("correlation_factor"),
            "uncertainty_multiplier": rep.get("uncertainty_multiplier"),
            "total_p50": rep.get("total_p50"),
            "total_p90": rep.get("total_p90"),
        },
        "days": {
            "p50": days.get("p50"),
            "p90": days.get("p90"),
            "bull": days.get("bull"),
        },
        "scenarios": profit_result.get("scenarios", {}),
        "mpp": mpp_val,
        "required_discount": required_discount,
        "classification": classification.get("classification"),
        "loan_recommendation": classification.get("loan_recommendation"),
        "flags": classification.get("flags", []),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def write_jsonl(records: list[dict[str, Any]], filepath: str = "deals.jsonl") -> None:
    """Write audit records to a JSONL file.

    Args:
        records: List of audit dicts.
        filepath: Output file path.
    """
    path = Path(filepath)
    with open(path, "w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    logger.info("Wrote %d records to %s", len(records), path)


def write_csv(records: list[dict[str, Any]], filepath: str = "deals.csv") -> None:
    """Write top deals sorted by profit_base to CSV.

    Args:
        records: List of audit dicts.
        filepath: Output file path.
    """
    if not records:
        return

    # Sort by base profit (80% loan scenario)
    sorted_records = sorted(
        records,
        key=lambda r: r.get("scenarios", {}).get("80pct_loan", {}).get("profit_base", 0),
        reverse=True,
    )

    columns = [
        "listing_id", "make", "model", "variant", "year", "km", "price_nok",
        "location", "classification", "profit_base_80", "profit_bear_80",
        "roe_base_80", "mpp", "required_discount", "n_comps", "tier",
        "days_p50", "loan_recommendation", "listing_url",
    ]

    path = Path(filepath)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()

        for r in sorted_records:
            s80 = r.get("scenarios", {}).get("80pct_loan", {})
            writer.writerow({
                "listing_id": r.get("listing_id"),
                "make": r.get("make"),
                "model": r.get("model"),
                "variant": r.get("variant"),
                "year": r.get("year"),
                "km": r.get("km"),
                "price_nok": r.get("price_nok"),
                "location": r.get("location"),
                "classification": r.get("classification"),
                "profit_base_80": s80.get("profit_base"),
                "profit_bear_80": s80.get("profit_bear"),
                "roe_base_80": s80.get("roe_base"),
                "mpp": r.get("mpp"),
                "required_discount": r.get("required_discount"),
                "n_comps": r.get("comps", {}).get("n_comps"),
                "tier": r.get("comps", {}).get("tier"),
                "days_p50": r.get("days", {}).get("p50"),
                "loan_recommendation": r.get("loan_recommendation"),
                "listing_url": r.get("listing_url"),
            })

    logger.info("Wrote %d records to %s", len(sorted_records), path)
