"""FMV adjustments based on listing text analysis and SVV data."""

import logging
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

CONFIG_DIR = Path(__file__).parent.parent.parent / "config"


def _load_catalog() -> dict[str, Any]:
    """Load adjustment patterns from issue catalog."""
    with open(CONFIG_DIR / "issue_catalog.yaml") as f:
        return yaml.safe_load(f)


def _parse_date(value: Any) -> date | None:
    """Parse common SVV/ISO date formats."""
    if not value:
        return None

    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue

    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def evaluate_eu_status(svv_data: dict, text_signals: dict) -> tuple[str, int]:
    """
    Returns (status, adjustment_nok).

    Priority:
    1. SVV data
    2. text signals
    3. default
    """
    today = date.today()

    # Priority 1: SVV data
    frist_raw = svv_data.get("eu_kontroll_frist")
    frist_date = _parse_date(frist_raw)
    if frist_date:
        if frist_date < today:
            return ("forfalt", -4000)

        days_until = (frist_date - today).days

        sist_raw = svv_data.get("eu_kontroll_sist")
        sist_date = _parse_date(sist_raw)
        if sist_date:
            if (today - sist_date).days <= 180:
                return ("godkjent_fersk", 2500)
            return ("godkjent", 0)

        if days_until > 365:
            return ("godkjent_fersk", 2500)
        if days_until > 180:
            return ("godkjent", 0)
        if days_until > 30:
            return ("nær_forfall", -1000)
        return ("snart_forfalt", -3000)

    # Priority 2: text signals
    if text_signals.get("fresh"):
        return ("godkjent_fersk", 2500)
    if text_signals.get("overdue"):
        return ("forfalt", -4000)

    # Default
    return ("ikke_nevnt", -1000)


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

    # EU-kontroll has dedicated logic with SVV priority.
    eu_cfg = adjustments_cfg.get("eu_kontroll")
    if isinstance(eu_cfg, dict):
        positive_patterns: list[str] = []
        overdue_patterns: list[str] = []
        for item_name, item_cfg in eu_cfg.items():
            if not isinstance(item_cfg, dict):
                continue
            patterns = item_cfg.get("patterns", [])
            if item_name in {"godkjent_fersk", "godkjent"}:
                positive_patterns.extend(patterns)
            if item_name in {"forfalt", "snart_forfalt", "nær_forfall"}:
                overdue_patterns.extend(patterns)

        text_signals = {
            "fresh": any(re.search(p, search_text) for p in positive_patterns),
            "overdue": any(re.search(p, search_text) for p in overdue_patterns),
        }
        status, amount = evaluate_eu_status(svv_data, text_signals)
        source = "SVV API" if _parse_date(svv_data.get("eu_kontroll_frist")) else ("listing_text" if any(text_signals.values()) else "default")
        found.append({
            "type": f"eu_kontroll_{status}",
            "amount": amount,
            "source": source,
        })
        matched_categories.add("eu_kontroll")

    for category, items in adjustments_cfg.items():
        if category in matched_categories or not isinstance(items, dict):
            continue

        best_match: dict[str, Any] | None = None

        for item_name, item_cfg in items.items():
            if not isinstance(item_cfg, dict):
                continue

            patterns = item_cfg.get("patterns", [])
            adjustment = item_cfg.get("adjustment", 0)

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
