"""EV / PHEV battery SOH sensitivity."""

SOH_ADJUSTMENTS = {
    95: 0, 92: 0, 90: 0,
    88: -8000, 85: -15000, 82: -25000,
    80: -35000, 75: -50000, 70: -65000,
}


def _build_rec(min_prof: int | None, expected: int) -> str:
    if min_prof is None:
        return "Ikke lønnsom selv med perfekt batteri"
    if expected >= min_prof + 5:
        return f"Sannsynligvis OK. Forventet SOH ~{expected}%, trenger ≥{min_prof}%."
    if expected >= min_prof:
        return f"Marginal. Forventet ~{expected}%, trenger ≥{min_prof}%. MÅ verifiseres."
    return f"Risikabelt. Forventet ~{expected}%, men trenger ≥{min_prof}%."


def calculate_soh_sensitivity(
    base_profit: float,
    is_ev: bool,
    soh_reported: float | None,
    make: str,
    model: str,
    year: int,
) -> dict:
    """Calculate SOH sensitivity for EV/PHEV when SOH is missing."""
    if not is_ev:
        return {"applicable": False}

    if soh_reported is not None:
        return {
            "applicable": True,
            "soh_reported": soh_reported,
            "soh_missing": False,
        }

    scenarios = {}
    min_profitable_soh = None
    for soh, adj in sorted(SOH_ADJUSTMENTS.items(), reverse=True):
        profit_at_soh = base_profit + adj
        scenarios[soh] = {
            "adjustment": adj,
            "profit": round(profit_at_soh),
            "profitable": profit_at_soh > 0,
        }
        if profit_at_soh > 0 and min_profitable_soh is None:
            min_profitable_soh = soh

    car_age = 2026 - int(year or 2026)
    m = model.lower()
    if "leaf" in m:
        deg_per_year = 3.5
    elif "i3" in m:
        deg_per_year = 2.5
    elif "outlander" in m:
        deg_per_year = 2.0
    else:
        deg_per_year = 1.8

    expected = round(max(100 - deg_per_year * car_age, 60))

    return {
        "applicable": True,
        "soh_reported": None,
        "soh_missing": True,
        "scenarios": scenarios,
        "min_profitable_soh": min_profitable_soh,
        "expected_soh": expected,
        "recommendation": _build_rec(min_profitable_soh, expected),
        "seller_question": "Hva er batteriets helsestatus (SOH)? Har du mulighet til å kjøre en batteritest (LeafSpy, Tesla app, etc)?",
    }


# Backward-compatible alias used by earlier code/tests.
def calculate_soh_scenarios(base_profit: float, base_fmv_p50: float, is_ev: bool, soh_reported: float | None, make: str, model: str, year: int) -> dict:
    del base_fmv_p50
    return calculate_soh_sensitivity(base_profit, is_ev, soh_reported, make, model, year)
