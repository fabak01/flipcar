"""FINN Pristips integration – two-step flow: vehicle lookup → price valuation."""

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
    """Step 1: Look up vehicle profile by regnr to get IDs for price query."""
    url = f"{API_BASE}/vehicles/price-valuation/{registration_number}?mileage={km}"
    resp = requests.get(url, headers=_HEADERS, timeout=timeout)
    if resp.status_code == 400 and "X-Client-Id" in resp.text:
        raise RuntimeError("Pristips: X-Client-Id header required")
    resp.raise_for_status()
    return resp.json()


def _extract_vehicle_params(profile: dict[str, Any], km: int) -> dict[str, Any]:
    """Extract query params from vehicle profile for price valuation."""
    def _val(field: str) -> Any:
        v = profile.get(field, {})
        if isinstance(v, dict):
            return v.get("value") or v.get("id")
        return v

    params: dict[str, Any] = {}

    make_data = profile.get("make", {})
    if isinstance(make_data, dict) and make_data.get("id"):
        params["makeId"] = make_data["id"]

    model_data = profile.get("model", {})
    if isinstance(model_data, dict) and model_data.get("id"):
        params["modelId"] = model_data["id"]

    year = _val("modelYear")
    if year:
        params["modelYear"] = int(year)

    params["mileage"] = km

    fuel = profile.get("engineFuel", {})
    if isinstance(fuel, dict) and fuel.get("id"):
        params["engineFuelId"] = fuel["id"]

    wd = profile.get("wheelDrive", {})
    if isinstance(wd, dict) and wd.get("id"):
        params["wheelDriveId"] = wd["id"]

    trans = profile.get("transmission", {})
    if isinstance(trans, dict) and trans.get("id"):
        params["transmissionId"] = trans["id"]

    reg_class = profile.get("registrationClass", {})
    if isinstance(reg_class, dict) and reg_class.get("id"):
        params["registrationClassId"] = reg_class["id"]

    body = profile.get("bodyType", {})
    if isinstance(body, dict) and body.get("id"):
        params["bodyTypeId"] = body["id"]

    return params


def _fetch_active(vehicle_params: dict[str, Any], timeout: int = 20) -> dict[str, Any] | None:
    """Fetch active ads summary (filtered by make/model)."""
    url = f"{API_BASE}/ads/active"
    resp = requests.get(url, params=vehicle_params, headers=_HEADERS, timeout=timeout)
    if resp.ok:
        return resp.json()
    return None


def _fetch_sold(vehicle_params: dict[str, Any], timeout: int = 20) -> dict[str, Any] | None:
    """Fetch sold ads summary (filtered by make/model)."""
    url = f"{API_BASE}/ads/sold"
    resp = requests.get(url, params=vehicle_params, headers=_HEADERS, timeout=timeout)
    if resp.ok:
        return resp.json()
    return None


def _fetch_publishing_time(vehicle_params: dict[str, Any], timeout: int = 20) -> dict[str, Any] | None:
    """Fetch publishing time summary (filtered by make/model)."""
    url = f"{API_BASE}/ads/distribution/publishing-time/summary"
    resp = requests.get(url, params=vehicle_params, headers=_HEADERS, timeout=timeout)
    if resp.ok:
        return resp.json()
    return None


def _fetch_price_percentile(vehicle_params: dict[str, Any], listing_price: int, timeout: int = 20) -> dict[str, Any] | None:
    """Try to get price percentile for a specific listing price."""
    url = f"{API_BASE}/ads/price/percentile"
    params = {**vehicle_params, "price": listing_price}
    resp = requests.get(url, params=params, headers=_HEADERS, timeout=timeout)
    if resp.ok:
        return resp.json()
    return None


def _build_result(
    registration_number: str,
    km: int,
    profile: dict[str, Any],
    active_data: dict[str, Any] | None,
    sold_data: dict[str, Any] | None,
    pub_time: dict[str, Any] | None,
) -> dict[str, Any]:
    """Combine all API responses into a unified market-anchor structure."""

    # Active similar ads
    active_total = None
    if active_data and isinstance(active_data, dict):
        active_total = active_data.get("activeTotal")

    # Sold count (last 90 days)
    sold_90d = None
    sold_30d = None
    if sold_data and isinstance(sold_data, dict):
        sold_90d = sold_data.get("last90Days") or sold_data.get("last90days")
        sold_30d = sold_data.get("last30days") or sold_data.get("last30Days")

    # Days to sell from publishing time summary
    days_to_sell = None
    if pub_time and isinstance(pub_time, dict):
        quarterly = pub_time.get("countDistributionQuarterly", [])
        if quarterly:
            latest = quarterly[-1]
            days_to_sell = latest.get("publishingTimeMedian") or latest.get("standingTimeMedian")

    return {
        "registration_number": registration_number,
        "km": km,
        "market_anchor_price": None,
        "market_anchor_low": None,
        "market_anchor_high": None,
        "market_days_to_sell": days_to_sell,
        "market_active_similar": active_total,
        "market_sold_90d": sold_90d,
        "market_sold_30d": sold_30d,
        "market_comps": [],
        "vehicle_profile": profile,
        "active_raw": active_data,
        "sold_raw": sold_data,
        "publishing_time_raw": pub_time,
        "source": "finn_pristips",
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


def get_pristips(registration_number: str, km: int, timeout: int = 20) -> dict[str, Any]:
    """Fetch FINN Pristips market data via two-step flow."""
    reg = registration_number.strip().upper().replace(" ", "")

    # Step 1: Vehicle lookup
    profile = _lookup_vehicle(reg, int(km), timeout)
    if not profile:
        raise RuntimeError(f"Pristips: no vehicle profile for {reg}")

    # Step 2: Extract params and query filtered endpoints
    vparams = _extract_vehicle_params(profile, int(km))
    logger.info("Pristips vehicle params for %s: %s", reg, vparams)

    active_data = _fetch_active(vparams, timeout)
    sold_data = _fetch_sold(vparams, timeout)
    pub_time = _fetch_publishing_time(vparams, timeout)

    result = _build_result(reg, int(km), profile, active_data, sold_data, pub_time)

    logger.info(
        "Pristips result for %s: days=%s, active=%s, sold_90d=%s, sold_30d=%s",
        reg, result["market_days_to_sell"], result["market_active_similar"],
        result["market_sold_90d"], result.get("market_sold_30d"),
    )

    return result


def get_pristips_cached(registration_number: str, km: int) -> dict[str, Any] | None:
    """Cache wrapper: do not fetch same regnr+km more than once per week."""
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
