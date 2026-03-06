"""General EV battery SOH sensitivity analysis."""

from datetime import date

SOH_ADJUSTMENTS = {
    95: 0,
    92: 0,
    90: 0,
    88: -8000,
    85: -15000,
    82: -25000,
    80: -35000,
    75: -50000,
    70: -65000,
}


def _expected_soh_for_age(make: str, model: str, year: int) -> tuple[int, int]:
    """Simple heuristic expected SOH range by age and model family."""
    age = max(date.today().year - int(year or date.today().year), 0)
    make_model = f"{make} {model}".lower()

    if "leaf" in make_model:
        annual_drop = 2.6
    elif "bmw" in make_model and "i3" in make_model:
        annual_drop = 1.8
    else:
        annual_drop = 1.4

    center = max(65, 100 - int(age * annual_drop))
    lower = max(60, center - 5)
    upper = min(100, center + 5)
    return lower, upper


def _build_recommendation(min_profitable_soh: int | None, min_soh_for_target_profit: int | None) -> str:
    """Build recommendation text from scenario thresholds."""
    if min_profitable_soh is None:
        return "ikke lønnsom selv med sterk batterihelse"
    if min_soh_for_target_profit is None:
        return "mulig lønnsom, men må verifisere SOH"
    if min_soh_for_target_profit <= 82:
        return "sannsynlig lønnsom"
    if min_soh_for_target_profit <= 90:
        return "må verifisere SOH"
    return "risikabel uten høy SOH"


def calculate_soh_scenarios(
    base_profit: float,
    base_fmv_p50: float,
    is_ev: bool,
    soh_reported: float | None,
    make: str,
    model: str,
    year: int,
) -> dict:
    """Calculate EV SOH sensitivity scenarios."""
    if not is_ev:
        return {"applicable": False}

    if soh_reported is not None:
        return {
            "applicable": True,
            "soh_reported": soh_reported,
            "soh_missing": False,
            "scenarios": None,
            "recommendation": None,
            "seller_question": None,
        }

    scenarios = []
    min_profitable_soh = None
    min_soh_for_target_profit = None

    for soh, adjustment in sorted(SOH_ADJUSTMENTS.items(), reverse=True):
        adjusted_fmv = base_fmv_p50 + adjustment
        profit_estimate = base_profit + adjustment
        scenarios.append({
            "soh": soh,
            "fmv_adjustment_nok": adjustment,
            "adjusted_fmv_p50": round(adjusted_fmv),
            "profit_estimate": round(profit_estimate),
        })
        if min_profitable_soh is None and profit_estimate >= 0:
            min_profitable_soh = soh
        if min_soh_for_target_profit is None and profit_estimate >= 20000:
            min_soh_for_target_profit = soh

    expected_low, expected_high = _expected_soh_for_age(make, model, year)
    recommendation = _build_recommendation(min_profitable_soh, min_soh_for_target_profit)

    return {
        "applicable": True,
        "soh_reported": None,
        "soh_missing": True,
        "scenarios": scenarios,
        "min_profitable_soh": min_profitable_soh,
        "min_soh_for_target_profit": min_soh_for_target_profit,
        "expected_soh_range": f"{expected_low}-{expected_high}%",
        "recommendation": recommendation,
        "seller_question": "Kan du oppgi dokumentert battery SOH (helst med bilde fra app/diagnose)?",
    }
