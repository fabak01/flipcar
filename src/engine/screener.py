"""Level A cheap listing screening."""

from typing import Any


def get_quick_cohort_median(listing: dict[str, Any], all_listings: list[dict[str, Any]]) -> int | None:
    """Fast median from same make/model and year ±2."""
    make = listing.get("make")
    model = listing.get("model")
    year = listing.get("year")
    listing_id = listing.get("listing_id")

    if not make or not model or not isinstance(year, int):
        return None

    cohort = [
        int(l["price_nok"]) for l in all_listings
        if l.get("make") == make
        and l.get("model") == model
        and isinstance(l.get("year"), int)
        and abs(l["year"] - year) <= 2
        and l.get("listing_id") != listing_id
        and isinstance(l.get("price_nok"), (int, float))
        and l["price_nok"] > 0
    ]

    if len(cohort) < 3:
        return None

    cohort_sorted = sorted(cohort)
    return cohort_sorted[len(cohort_sorted) // 2]


def screen_listing(listing: dict[str, Any], all_listings: list[dict[str, Any]], params: dict[str, Any]) -> dict[str, Any]:
    """Rask, billig screening uten API-kall."""
    score = 0.0
    reasons: list[str] = []

    if listing.get("registration_number"):
        score += 0.2
        reasons.append("har_regnr")

    if listing.get("dq_score", 0) >= 0.70:
        score += 0.1

    cohort_median = get_quick_cohort_median(listing, all_listings)
    if cohort_median and listing.get("price_nok"):
        under_median_threshold = params.get("screener", {}).get("under_median_threshold", 0.95)
        if listing["price_nok"] < cohort_median * under_median_threshold:
            price_discount = 1 - (listing["price_nok"] / cohort_median)
            score += min(price_discount * 2, 0.4)
            reasons.append(f"under_median_{price_discount:.0%}")

    text_len = len(listing.get("listing_text", ""))
    if text_len > 200:
        score += 0.1

    if listing.get("listing_age_days", 0) > 30:
        score += 0.1
        reasons.append("eldre_annonse")

    if listing.get("n_price_cuts", 0) > 0:
        score += 0.15
        reasons.append(f"prisredusert_{listing['n_price_cuts']}x")

    threshold = params.get("screener", {}).get("pass_threshold", 0.3)
    passes_screening = score >= threshold

    return {
        "shortlist_score": round(score, 2),
        "passes_screening": passes_screening,
        "screening_reasons": reasons,
        "cohort_median_used": cohort_median,
    }
