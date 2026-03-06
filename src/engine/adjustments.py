"""FMV adjustments based on listing text analysis and SVV data."""

import logging
import re
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

CONFIG_DIR = Path(__file__).parent.parent.parent / "config"


def _load_catalog() -> dict[str, Any]:
    """Load adjustment patterns from issue catalog."""
    with open(CONFIG_DIR / "issue_catalog.yaml") as f:
        return yaml.safe_load(f)


def detect_adjustments(
    listing: dict[str, Any],
    catalog: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Detect FMV adjustments by scanning listing text with regex patterns.

    Args:
        listing: Normalized listing dict with listing_text and optionally svv_data.
        catalog: Optional override for adjustment catalog.

    Returns:
        List of dicts with type, amount, and source fields.
    """
    if catalog is None:
        catalog = _load_catalog()

    adjustments_cfg = catalog.get("adjustments", {})
    text = (listing.get("listing_text") or "").lower()
    title = (listing.get("title") or "").lower()
    search_text = f"{title} {text}"
    svv_data = listing.get("svv_data", {})

    found: list[dict[str, Any]] = []
    matched_categories: set[str] = set()

    for category, items in adjustments_cfg.items():
        if not isinstance(items, dict):
            continue

        best_match: dict[str, Any] | None = None

        for item_name, item_cfg in items.items():
            if not isinstance(item_cfg, dict):
                continue

            patterns = item_cfg.get("patterns", [])
            adjustment = item_cfg.get("adjustment", 0)

            # EU kontroll: prefer SVV data
            if category == "eu_kontroll" and svv_data:
                if item_name == "godkjent_fersk" and svv_data.get("eu_kontroll_sist"):
                    best_match = {
                        "type": f"{category}_{item_name}",
                        "amount": adjustment,
                        "source": "SVV API",
                    }
                    break
                elif item_name == "forfalt" and svv_data.get("eu_kontroll_frist"):
                    # Check if expired (simple heuristic)
                    best_match = {
                        "type": f"{category}_{item_name}",
                        "amount": adjustment,
                        "source": "SVV API",
                    }
                    break

            for pattern in patterns:
                if re.search(pattern, search_text):
                    match = {
                        "type": f"{category}_{item_name}",
                        "amount": adjustment,
                        "source": "listing_text",
                    }
                    # For skade.bulk_riper, count occurrences (max 3)
                    if category == "skade" and item_name == "bulk_riper":
                        count = min(len(re.findall(pattern, search_text)), 3)
                        match["amount"] = adjustment * count
                        match["count"] = count

                    if best_match is None or abs(match["amount"]) > abs(best_match["amount"]):
                        best_match = match
                    break

        if best_match:
            matched_categories.add(category)
            found.append(best_match)

    # Default adjustments for categories with no match
    for category, items in adjustments_cfg.items():
        if category in matched_categories or not isinstance(items, dict):
            continue
        for item_name, item_cfg in items.items():
            if not isinstance(item_cfg, dict):
                continue
            if not item_cfg.get("patterns") and item_cfg.get("adjustment", 0) != 0:
                found.append({
                    "type": f"{category}_{item_name}",
                    "amount": item_cfg["adjustment"],
                    "source": "default",
                })
                break

    return found


def apply_adjustments(
    fmv: dict[str, Any],
    adjustments: list[dict[str, Any]],
) -> dict[str, Any]:
    """Apply adjustments to FMV values.

    Args:
        fmv: Dict with raw_p10, raw_p50, raw_p90.
        adjustments: List of adjustment dicts from detect_adjustments().

    Returns:
        Dict with adjusted_p10, adjusted_p50, adjusted_p90 and adjustment details.
    """
    total_adjustment = sum(a["amount"] for a in adjustments)
    negative_sum = sum(a["amount"] for a in adjustments if a["amount"] < 0)
    positive_sum = sum(a["amount"] for a in adjustments if a["amount"] > 0)

    adjusted_p50 = fmv["raw_p50"] + total_adjustment
    adjusted_p10 = fmv["raw_p10"] + negative_sum * 1.3
    adjusted_p90 = fmv["raw_p90"] + positive_sum * 0.7

    return {
        "adjusted_p10": round(adjusted_p10),
        "adjusted_p50": round(adjusted_p50),
        "adjusted_p90": round(adjusted_p90),
        "adjustments": adjustments,
        "total_adjustment": round(total_adjustment),
    }
