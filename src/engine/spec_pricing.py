"""Spec pricing layer: scan listing text for equipment/features and return NOK adjustments.

Uses config/spec_adjustments.yaml for explicit spec → NOK mappings.
Integrates into the adjustment layer in underwriting.py.
"""

import logging
import re
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

CONFIG_DIR = Path(__file__).parent.parent.parent / "config"


def _load_spec_config() -> dict[str, Any]:
    with open(CONFIG_DIR / "spec_adjustments.yaml") as f:
        return yaml.safe_load(f)


def detect_specs(
    listing: dict[str, Any],
    spec_config: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Scan listing text for spec features and return matched specs with NOK values.

    Args:
        listing: Normalized listing dict with listing_text, make, fuel_type, etc.
        spec_config: Optional override for spec config.

    Returns:
        List of dicts: {"spec": name, "matched_alias": alias, "amount_nok": int, "type": "positive"|"negative"}
    """
    if spec_config is None:
        spec_config = _load_spec_config()

    text = (listing.get("listing_text") or "").lower()
    title = (listing.get("title") or "").lower()
    search_text = f"{title} {text}"

    make = (listing.get("make") or "").strip()
    fuel_type = (listing.get("fuel_type") or "").lower()
    n_owners = listing.get("n_owners")

    matched: list[dict[str, Any]] = []

    # --- Positive specs ---
    for spec_name, spec_cfg in spec_config.get("positive_specs", {}).items():
        aliases = spec_cfg.get("aliases", [])

        # Filter by fuel type if applies_to is set
        applies_to = spec_cfg.get("applies_to")
        if applies_to and fuel_type not in [a.lower() for a in applies_to]:
            # Also check common Norwegian fuel type mappings
            fuel_map = {"el": "electric", "elektrisk": "electric", "plug-in hybrid": "plugin_hybrid"}
            mapped_fuel = fuel_map.get(fuel_type, fuel_type)
            if mapped_fuel not in [a.lower() for a in applies_to]:
                continue

        # Filter by make if applies_to_make is set
        applies_to_make = spec_cfg.get("applies_to_make")
        if applies_to_make and make not in applies_to_make:
            continue

        use_word_boundary = spec_cfg.get("word_boundary", False)

        for alias in aliases:
            alias_lower = alias.lower()
            if use_word_boundary:
                # Use regex word boundary to avoid substring matches
                pattern = r'\b' + re.escape(alias_lower) + r'\b'
                if re.search(pattern, search_text):
                    matched.append({
                        "spec": spec_name,
                        "matched_alias": alias,
                        "amount_nok": spec_cfg["premium_nok"],
                        "type": "positive",
                    })
                    break
            else:
                if alias_lower in search_text:
                    matched.append({
                        "spec": spec_name,
                        "matched_alias": alias,
                        "amount_nok": spec_cfg["premium_nok"],
                        "type": "positive",
                    })
                    break

    # --- Negative specs ---
    for spec_name, spec_cfg in spec_config.get("negative_specs", {}).items():
        # Special: high_owner_count uses threshold logic
        if spec_name == "high_owner_count":
            threshold = spec_cfg.get("threshold", 4)
            per_extra = spec_cfg.get("per_extra_owner_nok", -3000)
            if isinstance(n_owners, (int, float)) and n_owners > threshold:
                extra = int(n_owners) - threshold
                matched.append({
                    "spec": spec_name,
                    "matched_alias": f"{n_owners} eiere (>{threshold})",
                    "amount_nok": per_extra * extra,
                    "type": "negative",
                })
            continue

        aliases = spec_cfg.get("aliases", [])
        discount = spec_cfg.get("discount_nok", 0)

        for alias in aliases:
            if alias.lower() in search_text:
                matched.append({
                    "spec": spec_name,
                    "matched_alias": alias,
                    "amount_nok": discount,
                    "type": "negative",
                })
                break

    return matched


def summarize_specs(specs: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize spec detections into totals."""
    positive_total = sum(s["amount_nok"] for s in specs if s["type"] == "positive")
    negative_total = sum(s["amount_nok"] for s in specs if s["type"] == "negative")
    return {
        "specs_detected": specs,
        "n_positive": sum(1 for s in specs if s["type"] == "positive"),
        "n_negative": sum(1 for s in specs if s["type"] == "negative"),
        "total_positive_nok": positive_total,
        "total_negative_nok": negative_total,
        "net_spec_adjustment_nok": positive_total + negative_total,
    }
