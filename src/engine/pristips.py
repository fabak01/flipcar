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
DEBUG_DIR = Path(__file__).parent.parent.parent / "debug"

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
    """Open FINN Pristips with headless browser, fill in regnr + km, parse results.

    Three extraction strategies (in order):
    1. Intercept XHR API responses (most reliable)
    2. Parse innerText of rendered page (visible text)
    3. Parse inline <script> JSON data (embedded state)
    """
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
    captured_responses: list[dict[str, Any]] = []

    def _on_response(response):
        """Capture JSON API responses from FINN's internal endpoints."""
        url = response.url
        ct = response.headers.get("content-type", "")
        if "json" in ct and ("price-valuation" in url or "pristips" in url.lower()):
            try:
                body = response.json()
            except Exception:
                body = None
            captured_responses.append({"url": url, "status": response.status, "body": body})
            logger.debug("Captured XHR: %s (status=%d)", url, response.status)

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

            # Intercept all API responses
            page.on("response", _on_response)

            # Navigate
            page.goto(PRISTIPS_URL, wait_until="domcontentloaded", timeout=20000)
            page.wait_for_timeout(2000)

            # Accept cookies banner
            try:
                page.click("button:has-text('Godta alle')", timeout=2000)
            except Exception:
                pass

            # Fill regnr - try multiple selectors
            regnr_filled = False
            for selector in [
                'input#r0',
                'input[name="registration-number"]',
                'input[name*="registration"]',
                'input[placeholder*="registrering"]',
                'input[placeholder*="regnr"]',
                'input[aria-label*="Registreringsnummer"]',
                'input[type="text"]',
            ]:
                try:
                    el = page.locator(selector).first
                    if el.is_visible(timeout=1000):
                        el.fill(regnr)
                        regnr_filled = True
                        logger.debug("Filled regnr using selector: %s", selector)
                        break
                except Exception:
                    continue

            if not regnr_filled:
                logger.warning("Could not find regnr input field")
                browser.close()
                return None

            page.wait_for_timeout(500)

            # Fill km - try multiple selectors
            for selector in [
                'input#mileage',
                'input[name="mileage"]',
                'input[name*="mileage"]',
                'input[placeholder*="km"]',
                'input[placeholder*="Kilometerstand"]',
                'input[aria-label*="Kilometerstand"]',
            ]:
                try:
                    el = page.locator(selector).first
                    if el.is_visible(timeout=1000):
                        el.fill(str(km))
                        logger.debug("Filled km using selector: %s", selector)
                        break
                except Exception:
                    continue

            page.wait_for_timeout(500)

            # Submit - try multiple strategies
            submitted = False
            for selector in [
                'button:has-text("Sjekk bil")',
                'button:has-text("Sjekk")',
                'button:has-text("Hent")',
                'button[type="submit"]',
                'form button',
            ]:
                try:
                    btn = page.locator(selector).first
                    if btn.is_visible(timeout=1000):
                        btn.click()
                        submitted = True
                        logger.debug("Clicked submit using selector: %s", selector)
                        break
                except Exception:
                    continue

            if not submitted:
                page.keyboard.press("Enter")
                logger.debug("Pressed Enter as submit fallback")

            # Wait for results to load (watch for network idle or specific element)
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                page.wait_for_timeout(8000)

            # Extra wait for React rendering
            page.wait_for_timeout(3000)

            # Check for login redirect (cookies expired)
            current_url = page.url
            body_text = page.evaluate("document.body?.innerText || ''")
            if "login" in current_url or "auth" in current_url or "E-postadresse" in body_text:
                logger.warning("FINN cookies utloept. Kjoer: python scripts/get_finn_cookies.py")
                browser.close()
                return None

            # Fill km on result page if there's a separate km field
            for selector in [
                'input#mileage',
                'input[name="mileage"]',
                'input[placeholder*="km"]',
            ]:
                try:
                    el = page.locator(selector).first
                    if el.is_visible(timeout=1000):
                        current_val = el.input_value()
                        if not current_val or current_val == "0":
                            el.fill(str(km))
                            # Look for update button
                            for btn_sel in [
                                'button:has-text("Oppdater")',
                                'button:has-text("Beregn")',
                                'button:has-text("Sjekk")',
                            ]:
                                try:
                                    btn = page.locator(btn_sel).first
                                    if btn.is_visible(timeout=1000):
                                        btn.click()
                                        try:
                                            page.wait_for_load_state("networkidle", timeout=10000)
                                        except Exception:
                                            page.wait_for_timeout(5000)
                                        break
                                except Exception:
                                    continue
                        break
                except Exception:
                    continue

            # === EXTRACTION STRATEGY 1: XHR API responses ===
            result = _extract_from_xhr(captured_responses)
            if result and result.get("market_anchor_price"):
                logger.info("Pristips extracted via XHR for %s: price=%s", regnr, result["market_anchor_price"])
                browser.close()
                return result

            # === EXTRACTION STRATEGY 2: innerText parsing ===
            inner_text = page.evaluate("document.body?.innerText || ''")
            result = _parse_pristips_innertext(inner_text)
            if result and result.get("market_anchor_price"):
                logger.info("Pristips extracted via innerText for %s: price=%s", regnr, result["market_anchor_price"])
                browser.close()
                return result

            # === EXTRACTION STRATEGY 3: Inline script JSON ===
            html_content = page.content()
            result = _parse_pristips_script_json(html_content)
            if result and result.get("market_anchor_price"):
                logger.info("Pristips extracted via script JSON for %s: price=%s", regnr, result["market_anchor_price"])
                browser.close()
                return result

            # All strategies failed - save debug snapshot
            _save_debug_snapshot(regnr, km, html_content, inner_text, captured_responses)
            logger.warning("Pristips: alle extraherings-strategier feilet for %s. Debug lagret.", regnr)

            # Still try to return partial data from innerText (market data without price)
            if result:
                browser.close()
                return result

            browser.close()

    except Exception as e:
        logger.error("Browser-feil for %s: %s", regnr, e)
        return None

    return None


def _extract_from_xhr(responses: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Extract price estimate from intercepted XHR API responses."""
    result: dict[str, Any] = {}

    for resp in responses:
        body = resp.get("body")
        if not body or not isinstance(body, dict):
            continue

        # Look for price estimate in various response shapes
        for price_key in [
            "priceEstimate", "estimatedPrice", "price_estimate",
            "pricePrediction", "predictedPrice", "marketPrice",
            "valuationPrice", "value",
        ]:
            val = body.get(price_key)
            if isinstance(val, (int, float)) and 10000 < val < 10000000:
                result["market_anchor_price"] = int(val)
                break
            # Nested: {"priceEstimate": {"amount": 244000}}
            if isinstance(val, dict):
                amt = val.get("amount") or val.get("value") or val.get("price")
                if isinstance(amt, (int, float)) and 10000 < amt < 10000000:
                    result["market_anchor_price"] = int(amt)
                    break

        # Look for interval
        for lo_key, hi_key in [
            ("priceLow", "priceHigh"),
            ("priceMin", "priceMax"),
            ("lowEstimate", "highEstimate"),
            ("intervalLow", "intervalHigh"),
            ("confidenceIntervalLow", "confidenceIntervalHigh"),
        ]:
            lo = body.get(lo_key)
            hi = body.get(hi_key)
            if isinstance(lo, (int, float)) and isinstance(hi, (int, float)):
                if 10000 < lo < hi < 10000000:
                    result["market_anchor_low"] = int(lo)
                    result["market_anchor_high"] = int(hi)
                    break

        # Look for priceRange dict
        pr = body.get("priceRange") or body.get("priceInterval") or body.get("confidenceInterval")
        if isinstance(pr, dict):
            lo = pr.get("low") or pr.get("min") or pr.get("from")
            hi = pr.get("high") or pr.get("max") or pr.get("to")
            if isinstance(lo, (int, float)) and isinstance(hi, (int, float)) and 10000 < lo < hi:
                result["market_anchor_low"] = int(lo)
                result["market_anchor_high"] = int(hi)

        # Days to sell
        for days_key in ["daysToSell", "medianDaysToSell", "publishingTimeMedian", "estimatedDaysToSell"]:
            d = body.get(days_key)
            if isinstance(d, (int, float)) and 1 <= d <= 365:
                result["market_days_to_sell"] = int(d)
                break

        # Active / sold counts
        at = body.get("activeTotal") or body.get("activeCount")
        if isinstance(at, int):
            result["market_active_similar"] = at
        sold = body.get("last90Days") or body.get("soldLast90Days") or body.get("last90days")
        if isinstance(sold, int):
            result["market_sold_90d"] = sold

        # Recursively check nested structures
        for nested_key in ["result", "data", "valuation", "priceValuation", "estimation"]:
            nested = body.get(nested_key)
            if isinstance(nested, dict):
                sub = _extract_from_xhr([{"body": nested, "url": "", "status": 200}])
                if sub and sub.get("market_anchor_price"):
                    result.update(sub)

    return result if result else None


def _parse_pristips_innertext(text: str) -> dict[str, Any] | None:
    """Parse the visible innerText of the Pristips result page."""
    if not text or len(text) < 50:
        return None

    result: dict[str, Any] = {}

    # The price estimate typically appears as one of these patterns:
    # "244 000 kr" near "prisestimat" / "estimert" / "verdi"
    # "ca. 244 000 kr"

    lines = [l.strip() for l in text.split('\n') if l.strip()]

    # Strategy: find price estimate by context
    for i, line in enumerate(lines):
        ll = line.lower()
        # Look for indicator words
        if any(k in ll for k in ['prisestimat', 'estimert pris', 'estimert verdi', 'selg den selv']):
            # Price is usually on this line or nearby
            for j in range(max(0, i - 2), min(len(lines), i + 5)):
                price = _extract_price_from_text(lines[j])
                if price:
                    result["market_anchor_price"] = price
                    break
            if result.get("market_anchor_price"):
                break

    # Fallback: "ca. NNN NNN kr" anywhere
    if not result.get("market_anchor_price"):
        for line in lines:
            m = re.search(r'ca\.?\s*([\d\s]+\d)\s*kr', line)
            if m:
                val = _parse_nok(m.group(1))
                if 10000 < val < 10000000:
                    result["market_anchor_price"] = val
                    break

    # Fallback: large standalone price (NNN NNN kr) that looks like an estimate
    if not result.get("market_anchor_price"):
        for line in lines:
            m = re.search(r'\b(\d{2,3}\s\d{3})\s*kr\b', line)
            if m:
                val = _parse_nok(m.group(1))
                if 50000 < val < 5000000:
                    # Only use if this looks like a standalone price (not inside a sentence with other numbers)
                    other_nums = re.findall(r'\d{2,3}\s\d{3}', line)
                    if len(other_nums) <= 2:
                        result["market_anchor_price"] = val
                        break

    # Interval: "mellom X og Y" or "X – Y kr"
    for line in lines:
        m = re.search(r'mellom\s*([\d\s]+\d)\s*og\s*([\d\s]+\d)\s*kr', line)
        if m:
            result["market_anchor_low"] = _parse_nok(m.group(1))
            result["market_anchor_high"] = _parse_nok(m.group(2))
            break
        m = re.search(r'([\d\s]{5,}\d)\s*[-–]\s*([\d\s]{5,}\d)\s*kr', line)
        if m:
            lo = _parse_nok(m.group(1))
            hi = _parse_nok(m.group(2))
            if 10000 < lo < hi < 10000000:
                result["market_anchor_low"] = lo
                result["market_anchor_high"] = hi
                break

    # Days to sell
    for line in lines:
        m = re.search(r'(\d+)\s*dager', line)
        if m:
            d = int(m.group(1))
            if 1 <= d <= 365:
                result["market_days_to_sell"] = d
                break

    # Active: "X biler inn" or "X aktive"
    for line in lines:
        m = re.search(r'(\d+)\s*biler?\s*inn', line)
        if m:
            result["market_active_similar"] = int(m.group(1))
            break
        m = re.search(r'(\d+)\s*aktive', line.lower())
        if m:
            result["market_active_similar"] = int(m.group(1))
            break

    # Sold: "X biler ut" or "X solgt"
    for line in lines:
        m = re.search(r'(\d+)\s*biler?\s*ut', line)
        if m:
            result["market_sold_90d"] = int(m.group(1))
            break

    # Median, cheapest, most expensive
    for line in lines:
        ll = line.lower()
        if 'median' in ll:
            p = _extract_price_from_text(line)
            if p:
                result["comp_median"] = p
        if 'billigste' in ll:
            p = _extract_price_from_text(line)
            if p:
                result["comp_cheapest"] = p
        if 'dyreste' in ll:
            p = _extract_price_from_text(line)
            if p:
                result["comp_most_expensive"] = p

    result["market_comps"] = []
    return result if result else None


def _parse_pristips_script_json(html: str) -> dict[str, Any] | None:
    """Extract price data from inline <script> tags containing JSON state.

    FINN's React app often embeds initial state/props as base64 or JSON
    in script tags. This extracts price data from those.
    """
    import base64
    from bs4 import BeautifulSoup

    result: dict[str, Any] = {}
    soup = BeautifulSoup(html, "html.parser")

    for script in soup.find_all("script"):
        text = script.string or ""
        if not text:
            continue

        # Strategy A: base64-encoded state (like FINN search pages)
        if len(text) > 1000 and text.strip().startswith("eyJ"):
            try:
                decoded = base64.b64decode(text.strip()).decode("utf-8")
                data = json.loads(decoded)
                found = _search_json_for_price(data)
                if found:
                    result.update(found)
                    if result.get("market_anchor_price"):
                        return result
            except Exception:
                pass

        # Strategy B: __NEXT_DATA__ or similar JSON state
        if "__NEXT_DATA__" in (script.get("id") or ""):
            try:
                data = json.loads(text)
                found = _search_json_for_price(data)
                if found:
                    result.update(found)
                    if result.get("market_anchor_price"):
                        return result
            except Exception:
                pass

        # Strategy C: inline JSON objects with price keys
        for pattern in [
            r'"priceEstimate"\s*:\s*(\d+)',
            r'"estimatedPrice"\s*:\s*(\d+)',
            r'"pricePrediction"\s*:\s*(\d+)',
            r'"predictedPrice"\s*:\s*(\d+)',
            r'"valuationPrice"\s*:\s*(\d+)',
            r'"marketPrice"\s*:\s*(\d+)',
        ]:
            m = re.search(pattern, text)
            if m:
                val = int(m.group(1))
                if 10000 < val < 10000000:
                    result["market_anchor_price"] = val

        for pattern in [
            r'"priceLow"\s*:\s*(\d+).*?"priceHigh"\s*:\s*(\d+)',
            r'"intervalLow"\s*:\s*(\d+).*?"intervalHigh"\s*:\s*(\d+)',
            r'"confidenceIntervalLow"\s*:\s*(\d+).*?"confidenceIntervalHigh"\s*:\s*(\d+)',
        ]:
            m = re.search(pattern, text, re.DOTALL)
            if m:
                lo, hi = int(m.group(1)), int(m.group(2))
                if 10000 < lo < hi < 10000000:
                    result["market_anchor_low"] = lo
                    result["market_anchor_high"] = hi

        if result.get("market_anchor_price"):
            result["market_comps"] = []
            return result

    return result if result else None


def _search_json_for_price(data: Any, depth: int = 0) -> dict[str, Any] | None:
    """Recursively search a JSON structure for price estimate fields."""
    if depth > 10:
        return None
    if isinstance(data, dict):
        result: dict[str, Any] = {}
        for key, val in data.items():
            kl = key.lower()
            if kl in ("priceestimate", "estimatedprice", "priceprediction",
                       "predictedprice", "valuationprice", "marketprice"):
                if isinstance(val, (int, float)) and 10000 < val < 10000000:
                    result["market_anchor_price"] = int(val)
                elif isinstance(val, dict):
                    amt = val.get("amount") or val.get("value")
                    if isinstance(amt, (int, float)) and 10000 < amt < 10000000:
                        result["market_anchor_price"] = int(amt)
            if kl in ("pricelow", "pricemin", "intervallow", "lowestimate", "confidenceintervallow"):
                if isinstance(val, (int, float)) and val > 10000:
                    result["market_anchor_low"] = int(val)
            if kl in ("pricehigh", "pricemax", "intervalhigh", "highestimate", "confidenceintervalhigh"):
                if isinstance(val, (int, float)) and val > 10000:
                    result["market_anchor_high"] = int(val)
            if kl in ("daystosell", "mediandaystosell", "publishingtimemedian", "estimateddaystosell"):
                if isinstance(val, (int, float)) and 1 <= val <= 365:
                    result["market_days_to_sell"] = int(val)

        if result.get("market_anchor_price"):
            return result

        # Recurse into nested dicts/lists
        for val in data.values():
            if isinstance(val, (dict, list)):
                found = _search_json_for_price(val, depth + 1)
                if found and found.get("market_anchor_price"):
                    return found

    elif isinstance(data, list):
        for item in data:
            if isinstance(item, (dict, list)):
                found = _search_json_for_price(item, depth + 1)
                if found and found.get("market_anchor_price"):
                    return found

    return None


def _extract_price_from_text(text: str) -> int | None:
    """Extract a Norwegian price value from a text string."""
    # Match patterns like "244 000 kr", "244 000", "244000"
    m = re.search(r'(\d{2,3}[\s\xa0]\d{3})\s*(?:kr)?', text)
    if m:
        val = int(re.sub(r'[\s\xa0]', '', m.group(1)))
        if 10000 < val < 10000000:
            return val
    m = re.search(r'\b(\d{5,7})\b', text)
    if m:
        val = int(m.group(1))
        if 50000 < val < 5000000:
            return val
    return None


def _parse_nok(s: str) -> int:
    """Parse '244 000' to 244000."""
    return int(re.sub(r'[\s\xa0]+', '', s.strip()))


def _save_debug_snapshot(
    regnr: str, km: int, html: str, innertext: str, xhr: list[dict[str, Any]]
) -> None:
    """Save debug HTML + text + XHR responses for debugging failed extractions."""
    try:
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        prefix = f"{ts}_{regnr}"

        (DEBUG_DIR / f"{prefix}.html").write_text(html, encoding="utf-8")
        (DEBUG_DIR / f"{prefix}.txt").write_text(innertext, encoding="utf-8")

        xhr_safe = []
        for r in xhr:
            xhr_safe.append({
                "url": r.get("url", ""),
                "status": r.get("status"),
                "body": r.get("body"),
            })
        (DEBUG_DIR / f"{prefix}_xhr.json").write_text(
            json.dumps(xhr_safe, indent=2, default=str, ensure_ascii=False),
            encoding="utf-8",
        )
        logger.info("Debug snapshot saved to %s/%s.*", DEBUG_DIR, prefix)
    except Exception as e:
        logger.debug("Failed to save debug snapshot: %s", e)


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
        browser_result["registration_number"] = reg
        browser_result["km"] = km_int
        browser_result["fetched_at"] = datetime.now(timezone.utc).isoformat()
        browser_result["source"] = "finn_pristips_browser"
        logger.info("Pristips browser: price=%s for %s", browser_result["market_anchor_price"], reg)
        return browser_result

    # 2. Fallback to API
    api_result = _api_fallback(reg, km_int)
    if api_result:
        logger.info(
            "Pristips API fallback for %s: days=%s, active=%s",
            reg, api_result.get("market_days_to_sell"), api_result.get("market_active_similar"),
        )
        # Merge any partial browser data (e.g. days_to_sell from innertext)
        if browser_result:
            for k, v in browser_result.items():
                if v is not None and api_result.get(k) is None:
                    api_result[k] = v
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
