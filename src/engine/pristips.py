"""FINN Pristips integration.

Primary path: Playwright browser extraction (requires FINN login).
Fallback: public API endpoints for market data (no price estimate).

The browser path extracts the actual visible price estimate shown on
https://www.finn.no/mobility/insights/price-valuation after login.

The API fallback provides days-to-sell, active/sold counts, but NOT
the price estimate.
"""

import logging
from datetime import datetime, timezone
from typing import Any

import requests

from src.db.supabase_client import get_cached_pristips, upsert_pristips_cache

logger = logging.getLogger(__name__)

API_BASE = "https://www.finn.no/mobility/insights/price-valuation/api"
_HEADERS = {
    "X-Client-Id": "motor-price-valuation",
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
}


# ---------------------------------------------------------------------------
# Public API helpers (fallback, no price estimate)
# ---------------------------------------------------------------------------

def _lookup_vehicle(registration_number: str, km: int, timeout: int = 20) -> dict[str, Any] | None:
    """Look up vehicle profile by regnr."""
    url = f"{API_BASE}/vehicles/price-valuation/{registration_number}?mileage={km}"
    resp = requests.get(url, headers=_HEADERS, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def _extract_vehicle_params(profile: dict[str, Any], km: int) -> dict[str, Any]:
    """Extract query params from vehicle profile for market data endpoints."""
    def _id(field: str) -> Any:
        v = profile.get(field, {})
        return v.get("id") if isinstance(v, dict) else None

    def _val(field: str) -> Any:
        v = profile.get(field, {})
        return (v.get("value") or v.get("id")) if isinstance(v, dict) else v

    params: dict[str, Any] = {"mileage": km}
    for field, param in [
        ("make", "makeId"), ("model", "modelId"),
        ("engineFuel", "engineFuelId"), ("wheelDrive", "wheelDriveId"),
        ("transmission", "transmissionId"), ("registrationClass", "registrationClassId"),
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
    """GET JSON, return None on failure."""
    try:
        resp = requests.get(url, params=params, headers=_HEADERS, timeout=timeout)
        if resp.ok:
            return resp.json()
    except requests.RequestException as e:
        logger.debug("FINN API call failed: %s", e)
    return None


def _fetch_api_market_data(registration_number: str, km: int) -> dict[str, Any]:
    """Fetch market data from public API (no price estimate)."""
    reg = registration_number.strip().upper().replace(" ", "")
    profile = _lookup_vehicle(reg, int(km))
    if not profile:
        raise RuntimeError(f"Pristips API: no vehicle profile for {reg}")

    vparams = _extract_vehicle_params(profile, int(km))

    active = _fetch_json(f"{API_BASE}/ads/active", vparams)
    sold = _fetch_json(f"{API_BASE}/ads/sold", vparams)
    pub_time = _fetch_json(f"{API_BASE}/ads/distribution/publishing-time/summary", vparams)

    # Days to sell
    days_to_sell = None
    days_to_sell_90d = None
    if pub_time:
        days_to_sell = pub_time.get("publishingTimeMedianLast30Days")
        days_to_sell_90d = pub_time.get("publishingTimeMedianLast90Days")
        if days_to_sell is None:
            quarterly = pub_time.get("countDistributionQuarterly", [])
            if quarterly:
                days_to_sell = quarterly[-1].get("publishingTimeMedian")

    return {
        "registration_number": reg,
        "km": int(km),
        "market_anchor_price": None,  # API cannot provide this
        "market_anchor_low": None,
        "market_anchor_high": None,
        "market_days_to_sell": int(days_to_sell) if days_to_sell else None,
        "days_to_sell_90d": round(days_to_sell_90d, 1) if days_to_sell_90d else None,
        "market_active_similar": active.get("activeTotal") if active else None,
        "market_new_last_30d": active.get("last30days") if active else None,
        "market_sold_90d": (sold.get("last90Days") or sold.get("last90days")) if sold else None,
        "market_sold_last_30d": (sold.get("last30days") or sold.get("last30Days")) if sold else None,
        "market_comps": [],
        "vehicle_profile": {
            "make": profile.get("make", {}).get("text", ""),
            "model": profile.get("model", {}).get("text", ""),
            "year": profile.get("modelYear", {}).get("value"),
            "variants": profile.get("variants", []),
        },
        "source": "finn_pristips_api_fallback",
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def get_pristips(
    registration_number: str,
    km: int,
    force_refresh: bool = False,
) -> dict[str, Any]:
    """Fetch FINN Pristips data.

    Primary path: Playwright browser extraction (gets actual price estimate).
    Fallback: public API endpoints (market data only, no price estimate).

    Returns:
        {
            "market_anchor_price": int | None,
            "market_anchor_low": int | None,
            "market_anchor_high": int | None,
            "market_days_to_sell": int | None,
            "market_active_similar": int | None,
            "market_sold_90d": int | None,
            "market_comps": list,
            "source": "finn_pristips_browser" | "finn_pristips_api_fallback" | "cache",
            "raw_payload": dict,
            "fetched_at": iso_timestamp,
        }
    """
    reg = registration_number.strip().upper().replace(" ", "")
    km_int = int(km)

    # 1. Try browser extraction (primary path)
    try:
        from src.engine.pristips_browser import extract_pristips_browser
        browser_result = extract_pristips_browser(reg, km_int)
        if browser_result and browser_result.get("market_anchor_price"):
            logger.info(
                "Pristips browser: price=%s for %s",
                browser_result["market_anchor_price"], reg,
            )
            return browser_result
        elif browser_result:
            logger.info("Browser ran but no price estimate extracted for %s", reg)
    except ImportError:
        logger.debug("Playwright not available, skipping browser extraction")
    except Exception as e:
        logger.warning("Browser extraction failed for %s: %s", reg, e)

    # 2. Fallback to public API
    try:
        api_result = _fetch_api_market_data(reg, km_int)
        logger.info(
            "Pristips API fallback for %s: days=%s, active=%s, sold_90d=%s",
            reg, api_result.get("market_days_to_sell"),
            api_result.get("market_active_similar"),
            api_result.get("market_sold_90d"),
        )
        return api_result
    except Exception as e:
        logger.warning("Pristips API fallback also failed for %s: %s", reg, e)
        return {
            "market_anchor_price": None,
            "market_anchor_low": None,
            "market_anchor_high": None,
            "market_days_to_sell": None,
            "market_active_similar": None,
            "market_sold_90d": None,
            "market_comps": [],
            "source": "failed",
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }


def get_pristips_cached(
    registration_number: str,
    km: int,
    force_refresh: bool = False,
) -> dict[str, Any] | None:
    """Cache wrapper: reuse cached result within 7 days."""
    if not force_refresh:
        cached = get_cached_pristips(registration_number, int(km), max_age_days=7)
        if cached:
            cached["source"] = "cache"
            return cached

    result = get_pristips(registration_number, int(km), force_refresh=force_refresh)
    if result and result.get("source") != "failed":
        upsert_pristips_cache(registration_number, int(km), result)

    return result if result.get("source") != "failed" else None
