"""Repair cost estimator: Layer 1 (text issues) + Layer 2 (model-specific)."""

import logging
import re
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

CONFIG_DIR = Path(__file__).parent.parent.parent / "config"


def _load_issue_catalog() -> dict[str, Any]:
    """Load issue patterns from catalog."""
    with open(CONFIG_DIR / "issue_catalog.yaml") as f:
        return yaml.safe_load(f)


def _load_model_issues() -> dict[str, Any]:
    """Load model-specific issues."""
    with open(CONFIG_DIR / "model_issues.yaml") as f:
        return yaml.safe_load(f)


def _load_params() -> dict[str, Any]:
    """Load rep estimator parameters."""
    with open(CONFIG_DIR / "params.yaml") as f:
        return yaml.safe_load(f)


def _model_key(make: str, model: str) -> str:
    """Generate config key from make+model."""
    return f"{make}_{model}".lower().replace(" ", "_").replace(".", "").replace("-", "")


def detect_text_issues(
    listing: dict[str, Any],
    catalog: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Layer 1: Detect explicit repair issues from listing text.

    Args:
        listing: Normalized listing dict.
        catalog: Optional override for issue catalog.

    Returns:
        List of detected issues with name, p50, p90.
    """
    if catalog is None:
        catalog = _load_issue_catalog()

    issues_cfg = catalog.get("issues", {})
    text = (listing.get("listing_text") or "").lower()
    title = (listing.get("title") or "").lower()
    search_text = f"{title} {text}"

    detected: list[dict[str, Any]] = []

    for issue_name, issue_cfg in issues_cfg.items():
        for pattern in issue_cfg.get("patterns", []):
            if re.search(pattern, search_text):
                detected.append({
                    "name": issue_name,
                    "p50": issue_cfg["p50"],
                    "p90": issue_cfg["p90"],
                    "matched_pattern": pattern,
                })
                break

    return detected


def get_model_issues(
    listing: dict[str, Any],
    model_issues: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Layer 2: Get model-specific known issues with probabilities.

    Args:
        listing: Normalized listing dict.
        model_issues: Optional override for model issues config.

    Returns:
        List of model issues with probability, cost_p50, cost_p90.
    """
    if model_issues is None:
        model_issues = _load_model_issues()

    make = listing.get("make", "")
    model = listing.get("model", "")
    year = listing.get("year")
    variant = listing.get("variant", "unknown")
    key = _model_key(make, model)

    model_cfg = model_issues.get(key)
    if not model_cfg:
        return []

    applicable_years = model_cfg.get("years", [])
    if year and year not in applicable_years:
        return []

    issues: list[dict[str, Any]] = []
    for issue in model_cfg.get("issues", []):
        prob = issue["probability"]
        cost_p50 = issue["cost_p50"]
        cost_p90 = issue["cost_p90"]

        # Apply special rules
        special_rules = model_cfg.get("special_rules", [])
        for rule in special_rules:
            rule_lower = rule.lower()
            if "2014" in rule_lower and "1.8" in rule_lower and year == 2014:
                prob *= 1.8
            if "30kwh" in rule_lower and "1.3" in rule_lower and variant == "30kwh":
                prob *= 1.3

        issues.append({
            "name": issue["name"],
            "probability": min(prob, 1.0),
            "cost_p50": cost_p50,
            "cost_p90": cost_p90,
            "confidence": issue.get("confidence", "MEDIUM"),
            "source": issue.get("source", ""),
        })

    return issues


def estimate_repairs(
    listing: dict[str, Any],
    catalog: dict[str, Any] | None = None,
    model_issues_cfg: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Full repair estimate combining Layer 1 + Layer 2.

    Args:
        listing: Normalized listing dict.
        catalog: Optional issue catalog override.
        model_issues_cfg: Optional model issues override.
        params: Optional params override.

    Returns:
        Dict with total_p50, total_p90, breakdown, and multipliers.
    """
    if params is None:
        params = _load_params()

    rep_params = params["rep"]

    # Layer 1
    text_issues = detect_text_issues(listing, catalog)
    lag1_p50 = sum(i["p50"] for i in text_issues)
    lag1_p90 = sum(i["p90"] for i in text_issues)

    # Layer 2
    model_iss = get_model_issues(listing, model_issues_cfg)
    lag2_expected = sum(i["probability"] * i["cost_p50"] for i in model_iss)
    lag2_p90 = sum(i["probability"] * i["cost_p90"] for i in model_iss)

    # Correlation factor
    n_text_issues = len(text_issues)
    if n_text_issues >= 3:
        correlation_factor = rep_params.get("correlation_3_plus_issues", 1.40)
    elif n_text_issues >= 2:
        correlation_factor = rep_params.get("correlation_2_issues", 1.20)
    else:
        correlation_factor = 1.0

    # Uncertainty multiplier
    uncertainty_multiplier = 1.0
    text = (listing.get("listing_text") or "").lower()
    flags: list[str] = []

    if "ingen servicehistorikk" in text or "ingen servicepapirer" in text or "uten service" in text:
        uncertainty_multiplier *= rep_params.get("uncertainty_no_service", 1.25)
        flags.append("ingen_servicehistorikk")

    if "selges som den er" in text or "as is" in text or "uten garanti" in text:
        uncertainty_multiplier *= rep_params.get("uncertainty_as_is", 1.40)
        flags.append("selges_som_den_er")

    text_len = len(listing.get("listing_text") or "")
    short_threshold = rep_params.get("short_text_threshold", 100)
    if text_len < short_threshold:
        uncertainty_multiplier *= rep_params.get("uncertainty_short_text", 1.15)
        flags.append("short_listing_text")

    dq_score = listing.get("dq_score", 1.0)
    if dq_score < 0.70:
        uncertainty_multiplier *= rep_params.get("uncertainty_low_dq", 1.20)
        flags.append("low_dq_score")

    # Nissan Leaf hard gate
    make = listing.get("make", "")
    model_name = listing.get("model", "")
    if _model_key(make, model_name) == "nissan_leaf":
        soh_mentioned = any("soh" in p.get("matched_pattern", "") for p in text_issues)
        if not soh_mentioned and "soh" not in text and "batterikapasitet" not in text:
            uncertainty_multiplier = max(uncertainty_multiplier, 0.80)
            flags.append("nissan_leaf_no_soh")

    # Totals
    rep_p50 = lag1_p50 + lag2_expected
    rep_p90 = (lag1_p90 + lag2_p90) * correlation_factor * uncertainty_multiplier

    return {
        "lag1_issues": text_issues,
        "lag2_issues": model_iss,
        "lag1_p50": round(lag1_p50),
        "lag1_p90": round(lag1_p90),
        "lag2_expected": round(lag2_expected),
        "lag2_p90": round(lag2_p90),
        "correlation_factor": correlation_factor,
        "uncertainty_multiplier": round(uncertainty_multiplier, 2),
        "total_p50": round(rep_p50),
        "total_p90": round(rep_p90),
        "flags": flags,
    }
