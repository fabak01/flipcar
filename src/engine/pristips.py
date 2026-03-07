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


def _fetch_price_valuation(vehicle_params: dict[str, Any], timeout: int = 20) -> dict[str, Any] | None:
    """Step 2: Query price valuation endpoint with vehicle attributes."""
    url = f"{API_BASE}/ads/price/valuation"
    resp = requests.get(url, params=vehicle_params, headers=_HEADERS, timeout=timeout)
    if resp.ok:
        return resp.json()
    logger.warning("Price valuation returned %d: %s", resp.status_code, resp.text[:200])
    return None


def _fetch_price_summary(vehicle_params: dict[str, Any], timeout: int = 20) -> dict[str, Any] | None:
    """Fallback: price summary (distribution stats)."""
    url = f"{API_BASE}/ads/distribution/price/summary"
    resp = requests.get(url, params=vehicle_params, headers=_HEADERS, timeout=timeout)
    if resp.ok:
        return resp.json()
    return None


def _fetch_ads_count(vehicle_params: dict[str, Any], timeout: int = 20) -> int | None:
    """Fetch count of active similar ads."""
    url = f"{API_BASE}/ads/count"
    resp = requests.get(url, params=vehicle_params, headers=_HEADERS, timeout=timeout)
    if resp.ok:
        data = resp.json()
        if isinstance(data, int):
            return data
        if isinstance(data, dict):
            return data.get("count") or data.get("total")
    return None


def _fetch_sold_count(vehicle_params: dict[str, Any], timeout: int = 20) -> dict[str, Any] | None:
    """Fetch sold ads summary."""
    url = f"{API_BASE}/ads/sold"
    resp = requests.get(url, params=vehicle_params, headers=_HEADERS, timeout=timeout)
    if resp.ok:
        return resp.json()
    return None


def _fetch_publishing_time(vehicle_params: dict[str, Any], timeout: int = 20) -> dict[str, Any] | None:
    """Fetch publishing time summary (days to sell)."""
    url = f"{API_BASE}/ads/distribution/publishing-time/summary"
    resp = requests.get(url, params=vehicle_params, headers=_HEADERS, timeout=timeout)
    if resp.ok:
        return resp.json()
    return None


def _build_result(
    registration_number: str,
    km: int,
    profile: dict[str, Any],
    price_val: dict[str, Any] | None,
    price_summary: dict[str, Any] | None,
    ads_count: int | None,
    sold_data: dict[str, Any] | None,
    pub_time: dict[str, Any] | None,
) -> dict[str, Any]:
    """Combine all API responses into a unified market-anchor structure."""

    # Price: try price_valuation first, then price_summary, then profile
    anchor = None
    low = None
    high = None

    if price_val:
        anchor = price_val.get("price") or price_val.get("median") or price_val.get("estimatedPrice") or price_val.get("valuation")
        low = price_val.get("priceLow") or price_val.get("low") or price_val.get("p25") or price_val.get("percentile25")
        high = price_val.get("priceHigh") or price_val.get("high") or price_val.get("p75") or price_val.get("percentile75")
        # If response is nested
        if not anchor and isinstance(price_val.get("result"), dict):
            r = price_val["result"]
            anchor = r.get("price") or r.get("median") or r.get("estimatedPrice")
            low = low or r.get("low") or r.get("p25")
            high = high or r.get("high") or r.get("p75")

    if not anchor and price_summary:
        anchor = price_summary.get("median") or price_summary.get("average") or price_summary.get("mean")
        low = low or price_summary.get("p25") or price_summary.get("percentile25") or price_summary.get("q1")
        high = high or price_summary.get("p75") or price_summary.get("percentile75") or price_summary.get("q3")
        if not anchor and isinstance(price_summary.get("summary"), dict):
            s = price_summary["summary"]
            anchor = s.get("median") or s.get("average")
            low = low or s.get("p25") or s.get("q1")
            high = high or s.get("p75") or s.get("q3")

    # Days to sell
    days_to_sell = None
    if pub_time:
        days_to_sell = pub_time.get("median") or pub_time.get("average") or pub_time.get("mean")
        if not days_to_sell and isinstance(pub_time.get("summary"), dict):
            days_to_sell = pub_time["summary"].get("median") or pub_time["summary"].get("average")

    # Sold count
    sold_90d = None
    if sold_data:
        if isinstance(sold_data, int):
            sold_90d = sold_data
        elif isinstance(sold_data, dict):
            sold_90d = sold_data.get("count") or sold_data.get("total") or sold_data.get("sold")
            if isinstance(sold_data.get("result"), (int, dict)):
                r = sold_data["result"]
                sold_90d = r if isinstance(r, int) else r.get("count") or r.get("total")

    return {
        "registration_number": registration_number,
        "km": km,
        "market_anchor_price": anchor,
        "market_anchor_low": low,
        "market_anchor_high": high,
        "market_days_to_sell": days_to_sell,
        "market_active_similar": ads_count,
        "market_sold_90d": sold_90d,
        "market_comps": [],
        "vehicle_profile": profile,
        "price_valuation_raw": price_val,
        "price_summary_raw": price_summary,
        "publishing_time_raw": pub_time,
        "sold_raw": sold_data,
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

    # Step 2: Extract params and query multiple endpoints
    vparams = _extract_vehicle_params(profile, int(km))
    logger.info("Pristips vehicle params for %s: %s", reg, vparams)

    price_val = _fetch_price_valuation(vparams, timeout)
    price_summary = _fetch_price_summary(vparams, timeout)
    ads_count = _fetch_ads_count(vparams, timeout)
    sold_data = _fetch_sold_count(vparams, timeout)
    pub_time = _fetch_publishing_time(vparams, timeout)

    result = _build_result(reg, int(km), profile, price_val, price_summary, ads_count, sold_data, pub_time)

    logger.info(
        "Pristips result for %s: anchor=%s, low=%s, high=%s, days=%s, active=%s, sold=%s",
        reg, result["market_anchor_price"], result["market_anchor_low"],
        result["market_anchor_high"], result["market_days_to_sell"],
        result["market_active_similar"], result["market_sold_90d"],
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
