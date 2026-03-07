"""FINN Pristips integration – vehicle lookup + market data endpoints."""

import logging
from datetime import datetime, timezone
from typing import Any

import requests

from src.db.supabase_client import get_cached_pristips, upsert_pristips_cache

logger = logging.getLogger(__name__)

API_BASE = "https://www.finn.no/mobility/insights/price-valuation/api"
CLIENT_ID = "motor-price-valuation"
_HEADERS = {
    "X-Client-Id": CLIENT_ID,
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
}


def _lookup_vehicle(registration_number: str, km: int, timeout: int = 20) -> dict[str, Any] | None:
    """Look up vehicle profile by regnr to get IDs for market queries."""
    url = f"{API_BASE}/vehicles/price-valuation/{registration_number}?mileage={km}"
    resp = requests.get(url, headers=_HEADERS, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def _extract_vehicle_params(profile: dict[str, Any], km: int) -> dict[str, Any]:
    """Extract query params from vehicle profile for market data endpoints."""
    def _id(field: str) -> Any:
        v = profile.get(field, {})
        if isinstance(v, dict):
            return v.get("id")
        return None

    def _val(field: str) -> Any:
        v = profile.get(field, {})
        if isinstance(v, dict):
            return v.get("value") or v.get("id")
        return v

    params: dict[str, Any] = {"mileage": km}

    for field, param in [
        ("make", "makeId"),
        ("model", "modelId"),
        ("engineFuel", "engineFuelId"),
        ("wheelDrive", "wheelDriveId"),
        ("transmission", "transmissionId"),
        ("registrationClass", "registrationClassId"),
        ("bodyType", "bodyTypeId"),
    ]:
        val = _id(field)
        if val is not None:
            params[param] = val

    year = _val("modelYear")
    if year:
        params["modelYear"] = int(year)

    return params


def _fetch_json(url: str, params: dict[str, Any], timeout: int = 20) -> dict[str, Any] | None:
    """GET JSON from FINN API, return None on failure."""
    try:
        resp = requests.get(url, params=params, headers=_HEADERS, timeout=timeout)
        if resp.ok:
            return resp.json()
    except requests.RequestException as e:
        logger.debug("FINN API call failed: %s", e)
    return None


def _build_result(
    registration_number: str,
    km: int,
    profile: dict[str, Any],
    active_data: dict[str, Any] | None,
    sold_data: dict[str, Any] | None,
    pub_time: dict[str, Any] | None,
) -> dict[str, Any]:
    """Combine all API responses into unified market data structure."""

    # Active similar ads
    active_total = None
    new_last_30d = None
    if active_data and isinstance(active_data, dict):
        active_total = active_data.get("activeTotal")
        new_last_30d = active_data.get("last30days")

    # Sold count
    sold_90d = None
    sold_30d = None
    if sold_data and isinstance(sold_data, dict):
        sold_90d = sold_data.get("last90Days") or sold_data.get("last90days")
        sold_30d = sold_data.get("last30days") or sold_data.get("last30Days")

    # Days to sell
    days_to_sell = None
    days_to_sell_90d = None
    if pub_time and isinstance(pub_time, dict):
        days_to_sell = pub_time.get("publishingTimeMedianLast30Days")
        days_to_sell_90d = pub_time.get("publishingTimeMedianLast90Days")
        if days_to_sell is None:
            quarterly = pub_time.get("countDistributionQuarterly", [])
            if quarterly:
                latest = quarterly[-1]
                days_to_sell = latest.get("publishingTimeMedian") or latest.get("standingTimeMedian")

    # Vehicle info from profile
    make_text = profile.get("make", {}).get("text", "")
    model_text = profile.get("model", {}).get("text", "")
    year_val = profile.get("modelYear", {}).get("value")
    variants = profile.get("variants", [])

    return {
        "registration_number": registration_number,
        "km": km,
        "vehicle_make": make_text,
        "vehicle_model": model_text,
        "vehicle_year": year_val,
        "vehicle_variants": variants,
        "days_to_sell": int(days_to_sell) if days_to_sell else None,
        "days_to_sell_90d": round(days_to_sell_90d, 1) if days_to_sell_90d else None,
        "active_similar": active_total,
        "new_last_30d": new_last_30d,
        "sold_90d": sold_90d,
        "sold_last_30d": sold_30d,
        "publishing_time_raw": pub_time,
        "source": "finn_pristips",
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


def get_pristips(registration_number: str, km: int, timeout: int = 20) -> dict[str, Any]:
    """Fetch FINN Pristips market data."""
    reg = registration_number.strip().upper().replace(" ", "")

    profile = _lookup_vehicle(reg, int(km), timeout)
    if not profile:
        raise RuntimeError(f"Pristips: no vehicle profile for {reg}")

    vparams = _extract_vehicle_params(profile, int(km))
    logger.info("Pristips vehicle params for %s: %s", reg, vparams)

    active_data = _fetch_json(f"{API_BASE}/ads/active", vparams, timeout)
    sold_data = _fetch_json(f"{API_BASE}/ads/sold", vparams, timeout)
    pub_time = _fetch_json(f"{API_BASE}/ads/distribution/publishing-time/summary", vparams, timeout)

    result = _build_result(reg, int(km), profile, active_data, sold_data, pub_time)

    logger.info(
        "Pristips for %s: days=%s, active=%s, sold_90d=%s, sold_30d=%s",
        reg, result["days_to_sell"], result["active_similar"],
        result["sold_90d"], result["sold_last_30d"],
    )

    return result


def get_pristips_cached(registration_number: str, km: int) -> dict[str, Any] | None:
    """Cache wrapper: reuse cached result within 7 days."""
    cached = get_cached_pristips(registration_number, int(km), max_age_days=7)
    if cached:
        return cached

    try:
        fresh = get_pristips(registration_number, int(km))
    except Exception as e:
        logger.warning("Pristips fetch failed for %s: %s", registration_number, e)
        return None

    upsert_pristips_cache(registration_number, int(km), fresh)
    return fresh
