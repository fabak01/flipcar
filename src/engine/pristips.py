"""FINN Pristips integration (REST endpoint discovered in DEL A)."""

from datetime import datetime, timezone
from typing import Any

import requests

from src.db.supabase_client import get_cached_pristips, upsert_pristips_cache

BASE_URL = "https://www.finn.no/mobility/insights/price-valuation/api/vehicles/price-valuation"
CLIENT_ID = "motor-price-valuation"


def _normalize_pristips_response(data: dict[str, Any], registration_number: str, km: int) -> dict[str, Any]:
    """Map raw response to stable market-anchor structure."""
    # The discovered endpoint primarily returns structured vehicle valuation context.
    # Some market fields may be missing; keep schema stable with None defaults.
    anchor = data.get("marketPrice") or data.get("priceEstimate") or data.get("valuation")
    low = data.get("priceLow") or data.get("marketLow") or data.get("low")
    high = data.get("priceHigh") or data.get("marketHigh") or data.get("high")

    return {
        "registration_number": registration_number,
        "km": km,
        "market_anchor_price": anchor,
        "market_anchor_low": low,
        "market_anchor_high": high,
        "market_days_to_sell": data.get("daysToSell") or data.get("estimatedDaysToSell"),
        "market_active_similar": data.get("activeSimilar") or data.get("activeAds"),
        "market_sold_90d": data.get("sold90d") or data.get("soldLast90Days"),
        "market_comps": data.get("comparables") or data.get("comps") or [],
        "vehicle_profile": data,
        "source": "finn_pristips",
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


def get_pristips(registration_number: str, km: int, timeout: int = 20) -> dict[str, Any]:
    """Fetch FINN Pristips market-anchor data using confirmed REST endpoint."""
    reg = registration_number.strip().upper().replace(" ", "")
    url = f"{BASE_URL}/{reg}?mileage={int(km)}"
    headers = {
        "X-Client-Id": CLIENT_ID,
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0",
    }

    resp = requests.get(url, headers=headers, timeout=timeout)
    if resp.status_code == 400 and "X-Client-Id" in resp.text:
        raise RuntimeError('Pristips header missing: API returns {"error":"X-Client-Id header is required"}')
    resp.raise_for_status()
    payload = resp.json()
    return _normalize_pristips_response(payload, reg, int(km))


def get_pristips_cached(registration_number: str, km: int) -> dict[str, Any] | None:
    """Cache wrapper: do not fetch same regnr+km more than once per week."""
    cached = get_cached_pristips(registration_number, int(km), max_age_days=7)
    if cached:
        return cached

    try:
        fresh = get_pristips(registration_number, int(km))
    except Exception:
        return None

    upsert_pristips_cache(registration_number, int(km), fresh)
    return fresh
