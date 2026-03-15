"""FINN Pristips integration.

Strategy:
1. Check Supabase cache (7-day TTL)
2. If not cached: headless browser with saved cookies
3. If cookies missing/expired: API fallback (market activity only, no price)
4. If all fail: return None
"""

import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

from src.db.supabase_client import get_cached_pristips, upsert_pristips_cache

logger = logging.getLogger(__name__)

COOKIE_FILE = Path(__file__).parent.parent.parent / ".finn_cookies.json"
PRISTIPS_URL = "https://www.finn.no/mobility/insights/price-valuation"

API_BASE = "https://www.finn.no/mobility/insights/price-valuation/api"
_HEADERS = {
    "X-Client-Id": "motor-price-valuation",
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
}


# ---------------------------------------------------------------------------
# Browser extraction (primary path, requires cookies)
# ---------------------------------------------------------------------------

def _browser_extract(regnr: str, km: int) -> dict[str, Any] | None:
    """Open FINN Pristips with headless browser, fill in regnr + km, parse results."""
    if not COOKIE_FILE.exists():
        logger.warning("Ingen FINN cookies funnet. Kjoer: python scripts/get_finn_cookies.py")
        return None

    cookies = json.loads(COOKIE_FILE.read_text())

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logger.warning("Playwright ikke installert. Kjoer: pip install playwright && playwright install chromium")
        return None

    result = None

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-gpu"],
            )
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
                viewport={"width": 1280, "height": 900},
            )

            context.add_cookies(cookies)
            page = context.new_page()

            # Navigate
            page.goto(PRISTIPS_URL, wait_until="domcontentloaded", timeout=15000)
            page.wait_for_timeout(2000)

            # Accept cookies banner
            try:
                page.click("button:has-text('Godta alle')", timeout=2000)
            except Exception:
                pass

            # Fill regnr
            regnr_input = page.locator(
                'input[placeholder*="registrering"], input[name*="registration"], input#r0, input[type="text"]'
            ).first
            regnr_input.fill(regnr)
            page.wait_for_timeout(300)

            # Fill km (if visible on first page)
            km_input = page.locator(
                'input[placeholder*="km"], input[name*="mileage"], input#mileage'
            )
            if km_input.count() > 0:
                km_input.first.fill(str(km))
                page.wait_for_timeout(300)

            # Submit
            try:
                page.locator('button:has-text("Sjekk"), button[type="submit"]').first.click(timeout=3000)
            except Exception:
                page.keyboard.press("Enter")

            page.wait_for_timeout(5000)

            # Check for login redirect (cookies expired)
            current_url = page.url
            body_text = page.evaluate("document.body?.innerText || ''")
            if "login" in current_url or "auth" in current_url or "E-postadresse" in body_text:
                logger.warning("FINN cookies utloept. Kjoer: python scripts/get_finn_cookies.py")
                browser.close()
                return None

            # Fill km on result page if needed
            km_field = page.locator('input[placeholder*="km"], input[name*="mileage"], input#mileage')
            if km_field.count() > 0:
                km_field.first.fill(str(km))
                update_btn = page.locator('button:has-text("Oppdater")')
                if update_btn.count() > 0:
                    update_btn.first.click()
                    page.wait_for_timeout(5000)

            # Wait for full render
            page.wait_for_timeout(3000)

            # Parse
            content = page.content()
            result = _parse_pristips_html(content)

            browser.close()

    except Exception as e:
        logger.error("Browser-feil for %s: %s", regnr, e)
        return None

    if result:
        result["registration_number"] = regnr
        result["km"] = km
        result["fetched_at"] = datetime.now(timezone.utc).isoformat()
        result["source"] = "finn_pristips_browser"

    return result


def _parse_pristips_html(content: str) -> dict[str, Any] | None:
    """Parse Pristips result page HTML and extract all data fields."""
    result: dict[str, Any] = {}

    # Price estimate ("ca. 244 000 kr")
    price_match = re.search(r'ca\.?\s*([\d\s]+)\s*kr', content)
    if price_match:
        result["market_anchor_price"] = _parse_nok(price_match.group(1))

    # Interval ("mellom X og Y kr" or "X – Y kr")
    interval_match = re.search(r'mellom\s*([\d\s]+)\s*og\s*([\d\s]+)\s*kr', content)
    if not interval_match:
        interval_match = re.search(r'([\d\s]{5,})\s*[-–]\s*([\d\s]{5,})\s*kr', content)
    if interval_match:
        result["market_anchor_low"] = _parse_nok(interval_match.group(1))
        result["market_anchor_high"] = _parse_nok(interval_match.group(2))

    # Days to sell
    days_match = re.search(r'(?:Tid å selge|Dager å selge)[^<]*?(\d+)\s*dager', content)
    if not days_match:
        days_match = re.search(r'ca\.?\s*(\d+)\s*dager', content)
    if days_match:
        result["market_days_to_sell"] = int(days_match.group(1))

    # Active ("X biler inn")
    active_match = re.search(r'(\d+)\s*biler?\s*inn', content)
    if active_match:
        result["market_active_similar"] = int(active_match.group(1))

    # Sold ("X biler ut")
    sold_match = re.search(r'(\d+)\s*biler?\s*ut', content)
    if sold_match:
        result["market_sold_90d"] = int(sold_match.group(1))

    # Median
    median_match = re.search(r'Median[^<]*?([\d\s]+)\s*kr', content)
    if median_match:
        result["comp_median"] = _parse_nok(median_match.group(1))

    # Cheapest / Most expensive
    cheapest_match = re.search(r'Billigste[^<]*?([\d\s]+)\s*kr', content)
    if cheapest_match:
        result["comp_cheapest"] = _parse_nok(cheapest_match.group(1))
    expensive_match = re.search(r'Dyreste[^<]*?([\d\s]+)\s*kr', content)
    if expensive_match:
        result["comp_most_expensive"] = _parse_nok(expensive_match.group(1))

    # Comp count
    comp_count_match = re.search(r'(\d+)\s*biler?:', content)
    if comp_count_match:
        result["comp_count"] = int(comp_count_match.group(1))

    # New last 30 days
    new_30d_match = re.search(r'(\d+)\s*nye[^<]*?siste 30', content)
    if new_30d_match:
        result["market_new_last_30d"] = int(new_30d_match.group(1))

    # Sold last 30 days
    sold_30d_match = re.search(r'(\d+)\s*biler?\s*avpublisert[^<]*?siste 30', content)
    if sold_30d_match:
        result["market_sold_last_30d"] = int(sold_30d_match.group(1))

    if not result.get("market_anchor_price"):
        logger.warning("Kunne ikke finne prisestimat paa Pristips-siden")
        return None

    result["market_comps"] = []
    return result


def _parse_nok(s: str) -> int:
    """Parse '244 000' to 244000."""
    return int(re.sub(r'\s+', '', s.strip()))


# ---------------------------------------------------------------------------
# Public API fallback (no price estimate)
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


def _api_fallback(regnr: str, km: int) -> dict[str, Any] | None:
    """Fallback: public FINN API (market activity, NOT price estimate)."""
    reg = regnr.strip().upper().replace(" ", "")
    try:
        profile = _lookup_vehicle(reg, int(km))
    except Exception:
        return None
    if not profile:
        return None

    vparams = _extract_vehicle_params(profile, int(km))

    active = _fetch_json(f"{API_BASE}/ads/active", vparams)
    sold = _fetch_json(f"{API_BASE}/ads/sold", vparams)
    pub_time = _fetch_json(f"{API_BASE}/ads/distribution/publishing-time/summary", vparams)

    days_to_sell = None
    if pub_time:
        days_to_sell = pub_time.get("publishingTimeMedianLast30Days")
        if days_to_sell is None:
            quarterly = pub_time.get("countDistributionQuarterly", [])
            if quarterly:
                days_to_sell = quarterly[-1].get("publishingTimeMedian")

    result: dict[str, Any] = {
        "registration_number": reg,
        "km": int(km),
        "market_anchor_price": None,
        "market_anchor_low": None,
        "market_anchor_high": None,
        "market_days_to_sell": int(days_to_sell) if days_to_sell else None,
        "market_active_similar": active.get("activeTotal") if active else None,
        "market_new_last_30d": active.get("last30days") if active else None,
        "market_sold_90d": (sold.get("last90Days") or sold.get("last90days")) if sold else None,
        "market_sold_last_30d": (sold.get("last30days") or sold.get("last30Days")) if sold else None,
        "market_comps": [],
        "source": "finn_pristips_api_fallback",
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }

    return result if any(v is not None for k, v in result.items() if k.startswith("market_") and k != "market_comps") else None


# ---------------------------------------------------------------------------
# Main entry points
# ---------------------------------------------------------------------------

def get_pristips(registration_number: str, km: int) -> dict[str, Any] | None:
    """Fetch FINN Pristips data.

    Primary: headless browser with cookies (gets price estimate).
    Fallback: public API (market data only, no price).
    """
    reg = registration_number.strip().upper().replace(" ", "")
    km_int = int(km)

    # 1. Try browser extraction
    browser_result = _browser_extract(reg, km_int)
    if browser_result and browser_result.get("market_anchor_price"):
        logger.info("Pristips browser: price=%s for %s", browser_result["market_anchor_price"], reg)
        return browser_result

    # 2. Fallback to API
    api_result = _api_fallback(reg, km_int)
    if api_result:
        logger.info(
            "Pristips API fallback for %s: days=%s, active=%s",
            reg, api_result.get("market_days_to_sell"), api_result.get("market_active_similar"),
        )
        return api_result

    logger.warning("Pristips: ingen data for %s", reg)
    return None


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

    result = get_pristips(registration_number, int(km))
    if result:
        upsert_pristips_cache(registration_number, int(km), result)

    return result


# ---------------------------------------------------------------------------
# Smart batch: one lookup per model/variant/year/km-bucket
# ---------------------------------------------------------------------------

def get_pristips_batch_smart(
    listings: list[dict[str, Any]],
    registry: dict[str, str],
) -> dict[str, dict[str, Any]]:
    """Smart batching: group by model/variant/year, one lookup per 10k km bucket.

    Reduces 500 lookups to ~30-50 unique combinations.
    Returns {listing_id: pristips_result}.
    """
    from src.engine.regnr_registry import get_reference_regnr

    # Group by model/variant/year
    groups: dict[str, list[dict[str, Any]]] = {}
    for listing in listings:
        key = f"{listing.get('make', '')}_{listing.get('model', '')}_{listing.get('variant', 'unknown')}_{listing.get('year', 0)}"
        groups.setdefault(key, []).append(listing)

    results: dict[str, dict[str, Any]] = {}

    for _key, group_listings in groups.items():
        # Find a regnr for this group
        regnr = None
        for l in group_listings:
            if l.get("registration_number"):
                regnr = l["registration_number"]
                break
        if not regnr:
            sample = group_listings[0]
            regnr = get_reference_regnr(
                registry,
                sample.get("make", ""),
                sample.get("model", ""),
                sample.get("variant", "unknown"),
                sample.get("year", 0),
            )
        if not regnr:
            continue

        # Bucket km in 10k intervals
        km_buckets: dict[int, list[dict[str, Any]]] = {}
        for l in group_listings:
            km_val = l.get("km") or 0
            if km_val <= 0:
                continue
            bucket = round(km_val / 10000) * 10000
            bucket = max(bucket, 1000)  # avoid 0
            km_buckets.setdefault(bucket, []).append(l)

        # One lookup per km-bucket
        for km_bucket, bucket_listings in sorted(km_buckets.items()):
            pristips = get_pristips_cached(regnr, km_bucket)

            if pristips:
                for l in bucket_listings:
                    results[l["listing_id"]] = pristips

            # Rate limit between browser lookups
            time.sleep(3)

    return results
