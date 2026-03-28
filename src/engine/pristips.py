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

# Shared selectors used by both get_pristips() and debug_pristips.py
REGNR_SELECTORS = [
    'input#r0',
    'input[name="registration-number"]',
    'input[name*="registration"]',
    'input[placeholder*="registrering"]',
    'input[placeholder*="regnr"]',
    'input[aria-label*="Registreringsnummer"]',
]

KM_SELECTORS = [
    'input#mileage',
    'input[name="mileage"]',
    'input[name*="mileage"]',
    'input[placeholder*="km"]',
    'input[placeholder*="Kilometerstand"]',
    'input[aria-label*="Kilometerstand"]',
    'input[aria-label*="km"]',
]

SUBMIT_SELECTORS = [
    'button:has-text("Sjekk bil")',
    'button:has-text("Sjekk")',
    'button:has-text("Hent")',
    'button:has-text("Beregn")',
    'button[type="submit"]',
]

UPDATE_SELECTORS = [
    'button:has-text("Oppdater")',
    'button:has-text("Beregn")',
    'button:has-text("Sjekk")',
]


def run_pristips_browser_session(
    regnr: str,
    km: int,
    cookies: list[dict],
    headless: bool = True,
) -> dict[str, Any]:
    """Shared browser automation for Pristips — used by both get_pristips() and debug_pristips.py.

    Returns a dict with:
        - inner_text: str — rendered page text
        - html: str — full page HTML
        - captured_responses: list — intercepted XHR JSON responses
        - final_url: str — URL after form submit
        - screenshot_bytes: bytes | None — PNG screenshot (for debugging)
        - login_required: bool — True if cookies are expired
        - regnr_filled: bool
        - km_refilled: bool — True if km was re-entered on result page
        - error: str | None — error message if something went wrong
    """
    from playwright.sync_api import sync_playwright

    captured_responses: list[dict[str, Any]] = []

    def _on_response(response):
        url = response.url
        ct = response.headers.get("content-type", "")
        if "json" in ct:
            try:
                body = response.json()
            except Exception:
                body = None
            captured_responses.append({"url": url, "status": response.status, "body": body})
            logger.debug("Captured XHR: %s (status=%d)", url, response.status)

    out: dict[str, Any] = {
        "inner_text": "",
        "html": "",
        "captured_responses": [],
        "final_url": "",
        "screenshot_bytes": None,
        "login_required": False,
        "regnr_filled": False,
        "km_refilled": False,
        "error": None,
    }

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=headless)
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
                viewport={"width": 1280, "height": 900},
            )
            context.add_cookies(cookies)
            page = context.new_page()
            page.on("response", _on_response)

            # --- Navigate ---
            page.goto(PRISTIPS_URL, wait_until="domcontentloaded", timeout=20000)
            page.wait_for_timeout(2000)

            # Cookie banner
            try:
                page.click("button:has-text('Godta alle')", timeout=2000)
            except Exception:
                pass

            # --- Fill regnr ---
            for sel in REGNR_SELECTORS:
                try:
                    el = page.locator(sel).first
                    if el.is_visible(timeout=500):
                        el.fill(regnr)
                        out["regnr_filled"] = True
                        logger.debug("Filled regnr using selector: %s", sel)
                        break
                except Exception:
                    continue
            if not out["regnr_filled"]:
                # Last resort: first visible text input
                try:
                    el = page.locator('input[type="text"]').first
                    if el.is_visible(timeout=500):
                        el.fill(regnr)
                        out["regnr_filled"] = True
                        logger.debug("Filled regnr using fallback: input[type=text]")
                except Exception:
                    pass
            if not out["regnr_filled"]:
                out["error"] = "Could not find regnr input field"
                browser.close()
                return out

            page.wait_for_timeout(500)

            # --- Fill km (first attempt — may not be visible yet) ---
            km_filled_initial = False
            for sel in KM_SELECTORS:
                try:
                    el = page.locator(sel).first
                    if el.is_visible(timeout=500):
                        el.fill(str(km))
                        km_filled_initial = True
                        logger.debug("Filled km using selector: %s", sel)
                        break
                except Exception:
                    continue
            if not km_filled_initial:
                logger.debug("No km field found before submit (may appear after)")

            page.wait_for_timeout(500)

            # --- Submit ---
            submitted = False
            for sel in SUBMIT_SELECTORS:
                try:
                    btn = page.locator(sel).first
                    if btn.is_visible(timeout=500):
                        btn.click()
                        submitted = True
                        logger.debug("Clicked submit: %s", sel)
                        break
                except Exception:
                    continue
            if not submitted:
                page.keyboard.press("Enter")
                logger.debug("Pressed Enter as submit fallback")

            # --- Wait for results (match debug_pristips.py timing exactly) ---
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass
            page.wait_for_timeout(5000)  # Match debug_pristips.py: 5s extra wait

            # --- Check for login redirect ---
            out["final_url"] = page.url
            body_text = page.evaluate("document.body?.innerText || ''")
            if ("login" in out["final_url"] or "auth" in out["final_url"]
                    or "E-postadresse" in body_text or "Logg inn" in body_text):
                out["login_required"] = True
                out["inner_text"] = body_text
                browser.close()
                return out

            # --- Second km fill if needed (on result page) ---
            for sel in KM_SELECTORS[:3]:  # input#mileage, input[name=mileage], input[name*=mileage]
                try:
                    el = page.locator(sel).first
                    if el.is_visible(timeout=500):
                        cur_val = el.input_value()
                        if not cur_val or cur_val == "0":
                            el.fill(str(km))
                            out["km_refilled"] = True
                            logger.info("Re-filled km on result page: %s (was %r)", sel, cur_val)
                            for btn_sel in UPDATE_SELECTORS:
                                try:
                                    btn = page.locator(btn_sel).first
                                    if btn.is_visible(timeout=500):
                                        btn.click()
                                        logger.debug("Clicked update button: %s", btn_sel)
                                        try:
                                            page.wait_for_load_state("networkidle", timeout=10000)
                                        except Exception:
                                            pass
                                        page.wait_for_timeout(3000)
                                        break
                                except Exception:
                                    continue
                        break
                except Exception:
                    continue

            # --- Collect final page state ---
            out["inner_text"] = page.evaluate("document.body?.innerText || ''")
            out["html"] = page.content()
            out["captured_responses"] = captured_responses

            # Screenshot for debugging
            try:
                out["screenshot_bytes"] = page.screenshot(full_page=True)
            except Exception:
                pass

            browser.close()

    except Exception as e:
        out["error"] = str(e)

    return out


def _browser_extract(regnr: str, km: int, headless: bool = True) -> dict[str, Any] | None:
    """Open FINN Pristips with headless browser, fill in regnr + km, parse results.

    Uses the same browser automation as debug_pristips.py via run_pristips_browser_session().

    Four extraction strategies (in order):
    1. XHR /api/ads/price/valuation endpoint for PRICE (authoritative source)
    2. XHR other endpoints for MARKET ACTIVITY (days, active, sold)
    3. innerText parsing for PRICE (fallback if valuation XHR missing)
    4. Inline script JSON for PRICE (last resort)
    """
    logger.debug(f"[PRISTIPS] ENTER _browser_extract regnr={regnr} km={km} headless={headless}")

    if not COOKIE_FILE.exists():
        logger.debug(f"[PRISTIPS] _browser_extract: NO COOKIE FILE at {COOKIE_FILE}")
        logger.warning("Ingen FINN cookies funnet. Kjoer: python scripts/get_finn_cookies.py")
        return None

    cookies = json.loads(COOKIE_FILE.read_text())
    logger.debug(f"[PRISTIPS] _browser_extract: loaded {len(cookies)} cookies")

    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
    except ImportError:
        logger.debug("[PRISTIPS] _browser_extract: Playwright NOT installed")
        logger.warning("Playwright ikke installert. Kjoer: pip install playwright && playwright install chromium")
        return None

    # --- Run shared browser session ---
    logger.debug(f"[PRISTIPS] _browser_extract: calling run_pristips_browser_session(headless={headless})")
    session = run_pristips_browser_session(regnr, km, cookies, headless=headless)

    if session.get("error"):
        logger.debug(f"[PRISTIPS] _browser_extract: SESSION ERROR: {session['error']}")
        logger.error("Browser session error for %s: %s", regnr, session["error"])
        return None

    if session.get("login_required"):
        logger.debug("[PRISTIPS] _browser_extract: LOGIN REQUIRED (cookies expired)")
        logger.warning("FINN cookies utloept. Kjoer: python scripts/get_finn_cookies.py")
        return None

    inner_text = session["inner_text"]
    captured_responses = session["captured_responses"]

    logger.debug(f"[PRISTIPS] _browser_extract: session OK. final_url={session['final_url']}")
    logger.debug(f"[PRISTIPS] _browser_extract: captured_responses={len(captured_responses)}, innerText_len={len(inner_text)}")
    logger.debug(f"[PRISTIPS] _browser_extract: has 'ca.'={'ca.' in inner_text}, has 'mellom'={'mellom' in inner_text}")

    # Print ALL captured URLs
    for i, r in enumerate(captured_responses):
        url = r.get("url", "")
        is_valuation = "/api/ads/price/valuation" in url
        marker = " *** VALUATION ***" if is_valuation else ""
        logger.debug(f"[PRISTIPS]   XHR #{i}: {url[:150]}{marker}")

    # --- Conditionally save artifacts (controlled by pristips_debug.save_artifacts) ---
    _save_debug_if_enabled(regnr, km, session, extraction_failed=False)

    result: dict[str, Any] = {}

    # === STRATEGY 1: XHR /api/ads/price/valuation for PRICE (authoritative) ===
    logger.debug(f"[PRISTIPS] STRATEGY 1: _extract_valuation_from_xhr({len(captured_responses)} responses)")
    valuation = _extract_valuation_from_xhr(captured_responses)
    if valuation:
        result.update(valuation)
        logger.debug(f"[PRISTIPS] STRATEGY 1 SUCCESS: price={result['market_anchor_price']}, low={result.get('market_anchor_low')}, high={result.get('market_anchor_high')}")
    else:
        logger.debug("[PRISTIPS] STRATEGY 1 FAILED: no valuation endpoint matched")

    # === STRATEGY 2: XHR other endpoints for MARKET ACTIVITY (days, active, sold) ===
    xhr_activity = _extract_market_activity_from_xhr(captured_responses)
    if xhr_activity:
        for k, v in xhr_activity.items():
            if v is not None and result.get(k) is None:
                result[k] = v
        logger.debug(f"[PRISTIPS] STRATEGY 2: market activity keys={sorted(xhr_activity.keys())}")

    # === STRATEGY 3: innerText for PRICE (fallback if valuation XHR missing) ===
    if not result.get("market_anchor_price"):
        logger.debug("[PRISTIPS] STRATEGY 3: trying innerText fallback for price")
        innertext_data = _parse_pristips_innertext(inner_text)
        if innertext_data:
            for k, v in innertext_data.items():
                if v is not None and result.get(k) is None:
                    result[k] = v
            if result.get("market_anchor_price"):
                logger.debug(f"[PRISTIPS] STRATEGY 3 SUCCESS: price={result['market_anchor_price']}")
            else:
                logger.debug(f"[PRISTIPS] STRATEGY 3: innerText parsed but no price. keys={sorted(k for k,v in innertext_data.items() if v is not None)}")
        else:
            logger.debug("[PRISTIPS] STRATEGY 3: innerText parser returned None")

    # === STRATEGY 4: Inline script JSON for PRICE (last resort) ===
    if not result.get("market_anchor_price"):
        logger.debug("[PRISTIPS] STRATEGY 4: trying script JSON fallback for price")
        html_content = session["html"]
        script_data = _parse_pristips_script_json(html_content)
        if script_data:
            for k, v in script_data.items():
                if v is not None and result.get(k) is None:
                    result[k] = v
            if result.get("market_anchor_price"):
                logger.debug(f"[PRISTIPS] STRATEGY 4 SUCCESS: price={result.get('market_anchor_price')}")

    # Also merge any extra data from innerText (comp stats, days, etc.) even if we got price from XHR
    if result.get("market_anchor_price") and valuation:
        innertext_data = _parse_pristips_innertext(inner_text)
        if innertext_data:
            for k, v in innertext_data.items():
                if v is not None and result.get(k) is None:
                    result[k] = v

    if result.get("market_anchor_price"):
        logger.debug(f"[PRISTIPS] _browser_extract RETURNING: price={result['market_anchor_price']}, low={result.get('market_anchor_low')}, high={result.get('market_anchor_high')}")
        return result

    logger.debug(f"[PRISTIPS] _browser_extract: ALL STRATEGIES FAILED. result_keys={sorted(k for k,v in result.items() if v is not None) if result else 'empty'}")

    # Always save artifacts on failure regardless of toggle
    _save_debug_if_enabled(regnr, km, session, extraction_failed=True)

    # Return partial data (market activity without price) if we have any
    if result and any(v is not None for k, v in result.items() if k.startswith("market_")):
        logger.debug(f"[PRISTIPS] _browser_extract RETURNING partial (no price): keys={sorted(k for k,v in result.items() if v is not None)}")
        return result

    logger.debug("[PRISTIPS] _browser_extract RETURNING None")
    return None


def _extract_valuation_from_xhr(responses: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Extract price from the /api/ads/price/valuation XHR endpoint.

    This is the authoritative Pristips price source. The response shape is:
    {"prices": {"min": 222640.5625, "max": 245784.703125, "median": 234101.265625}, "occurrence": 9507}
    """
    logger.debug(f"[PRISTIPS] _extract_valuation_from_xhr: scanning {len(responses)} responses")
    for i, resp in enumerate(responses):
        url = resp.get("url", "")
        if "/api/ads/price/valuation" not in url:
            continue
        logger.debug(f"[PRISTIPS]   MATCH at #{i}: {url[:150]}")
        body = resp.get("body")
        if not body or not isinstance(body, dict):
            logger.debug(f"[PRISTIPS]   body missing or not dict: type={type(body)}")
            continue
        prices = body.get("prices")
        if not isinstance(prices, dict):
            logger.debug(f"[PRISTIPS]   no 'prices' dict in body. keys={list(body.keys())}")
            continue
        median = prices.get("median")
        lo = prices.get("min")
        hi = prices.get("max")
        logger.debug(f"[PRISTIPS]   raw prices: median={median}, min={lo}, max={hi}")
        if (isinstance(median, (int, float)) and isinstance(lo, (int, float))
                and isinstance(hi, (int, float))
                and 10000 < lo <= median <= hi < 10_000_000):
            result = {
                "market_anchor_price": round(median),
                "market_anchor_low": round(lo),
                "market_anchor_high": round(hi),
            }
            occurrence = body.get("occurrence")
            if isinstance(occurrence, (int, float)) and occurrence > 0:
                result["valuation_occurrence"] = int(occurrence)
            logger.debug(f"[PRISTIPS]   EXTRACTED: price={result['market_anchor_price']}, low={result['market_anchor_low']}, high={result['market_anchor_high']}, occurrence={occurrence}")
            return result
        else:
            logger.debug(f"[PRISTIPS]   VALIDATION FAILED: lo={lo} median={median} hi={hi}")
    logger.debug(f"[PRISTIPS] _extract_valuation_from_xhr: NO valuation URL found in {len(responses)} responses")
    return None


def _extract_market_activity_from_xhr(responses: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Extract ONLY market activity data from intercepted XHR API responses.

    Deliberately does NOT extract price — innerText is the reliable source for that.
    Only extracts: market_days_to_sell, market_active_similar, market_new_last_30d,
    market_sold_last_30d, market_sold_90d.
    """
    result: dict[str, Any] = {}

    for resp in responses:
        body = resp.get("body")
        if not body or not isinstance(body, dict):
            continue

        # Days to sell
        for days_key in ["daysToSell", "medianDaysToSell", "publishingTimeMedian", "estimatedDaysToSell"]:
            d = body.get(days_key)
            if isinstance(d, (int, float)) and 1 <= d <= 365:
                result["market_days_to_sell"] = int(d)
                break

        # Active counts
        at = body.get("activeTotal") or body.get("activeCount")
        if isinstance(at, int) and at >= 0:
            result["market_active_similar"] = at

        # New last 30 days
        new_30 = body.get("last30days") or body.get("last30Days") or body.get("newLast30Days")
        if isinstance(new_30, int) and new_30 >= 0:
            result["market_new_last_30d"] = new_30

        # Sold counts
        sold_90 = body.get("last90Days") or body.get("soldLast90Days") or body.get("last90days")
        if isinstance(sold_90, int) and sold_90 >= 0:
            result["market_sold_90d"] = sold_90

        sold_30 = body.get("soldLast30Days") or body.get("soldLast30days")
        if isinstance(sold_30, int) and sold_30 >= 0:
            result["market_sold_last_30d"] = sold_30

        # Recursively check nested structures (for activity data only)
        for nested_key in ["result", "data", "valuation", "priceValuation", "estimation"]:
            nested = body.get(nested_key)
            if isinstance(nested, dict):
                sub = _extract_market_activity_from_xhr([{"body": nested, "url": "", "status": 200}])
                if sub:
                    for k, v in sub.items():
                        if v is not None and result.get(k) is None:
                            result[k] = v

    return result if result else None


def _parse_pristips_innertext(text: str) -> dict[str, Any] | None:
    """Parse the visible innerText of the Pristips result page.

    Targets the main estimate section near 'Selg den selv' / 'Basert på maskinlæring'.
    Does NOT accidentally pick up later comps/distribution values (median, cheapest, etc.).
    """
    if not text or len(text) < 50:
        return None

    # --- Normalize text: \xa0 → space, collapse whitespace within lines ---
    text = text.replace('\xa0', ' ')
    lines_raw = text.split('\n')
    lines = [re.sub(r' {2,}', ' ', l.strip()) for l in lines_raw if l.strip()]

    result: dict[str, Any] = {}

    # --- Find the main estimate section ---
    # The price estimate appears near "Selg den selv på FINN" or "Prisestimat"
    # and before the comps/distribution section (marked by "Prisstatistikk",
    # "Median", "Billigste", "Dyreste", "Lignende biler til salgs").
    estimate_start = 0
    estimate_end = len(lines)

    for i, line in enumerate(lines):
        ll = line.lower()
        if any(k in ll for k in ['selg den selv', 'prisestimat', 'basert på maskinlæring']):
            estimate_start = max(0, i - 1)
            break

    for i, line in enumerate(lines):
        ll = line.lower()
        if i > estimate_start and any(k in ll for k in ['prisstatistikk', 'lignende biler til salgs', 'median']):
            estimate_end = i
            break

    estimate_lines = lines[estimate_start:estimate_end]

    # --- Extract anchor price: "ca. XXX XXX kr" in estimate section ---
    for line in estimate_lines:
        m = re.search(r'ca\.?\s*([\d\s]+\d)\s*kr', line)
        if m:
            val = _parse_nok(m.group(1))
            if 10000 < val < 10000000:
                result["market_anchor_price"] = val
                logger.debug("Pristips innerText anchor price: %d from line: %r", val, line)
                break

    # Fallback: any "NNN NNN kr" on a short line in estimate section (standalone price)
    if not result.get("market_anchor_price"):
        for line in estimate_lines:
            # Skip lines that contain "km" (mileage) or comp keywords
            if re.search(r'\bkm\b', line, re.IGNORECASE):
                continue
            m = re.search(r'\b(\d{2,3}\s\d{3})\s*kr\b', line)
            if m:
                val = _parse_nok(m.group(1))
                if 50000 < val < 5000000:
                    # Only short lines (likely standalone price, not a sentence with other numbers)
                    if len(line) < 30:
                        result["market_anchor_price"] = val
                        logger.debug("Pristips innerText anchor price (fallback): %d from line: %r", val, line)
                        break

    # --- Extract range: "mellom X og Y kr" in estimate section ---
    for line in estimate_lines:
        m = re.search(r'mellom\s*([\d\s]+\d)\s*og\s*([\d\s]+\d)\s*kr', line)
        if m:
            lo = _parse_nok(m.group(1))
            hi = _parse_nok(m.group(2))
            if 10000 < lo < hi < 10000000:
                result["market_anchor_low"] = lo
                result["market_anchor_high"] = hi
                logger.debug("Pristips innerText range: %d–%d from line: %r", lo, hi, line)
                break

    # Fallback: "X – Y kr" range pattern
    if not result.get("market_anchor_low"):
        for line in estimate_lines:
            m = re.search(r'([\d\s]{5,}\d)\s*[-–]\s*([\d\s]{5,}\d)\s*kr', line)
            if m:
                lo = _parse_nok(m.group(1))
                hi = _parse_nok(m.group(2))
                if 10000 < lo < hi < 10000000:
                    result["market_anchor_low"] = lo
                    result["market_anchor_high"] = hi
                    logger.debug("Pristips innerText range (dash): %d–%d from line: %r", lo, hi, line)
                    break

    # --- Days to sell (from full text, not just estimate section) ---
    for line in lines:
        m = re.search(r'(\d+)\s*dager', line)
        if m:
            d = int(m.group(1))
            if 1 <= d <= 365:
                result["market_days_to_sell"] = d
                break

    # --- Active: "X biler inn" or "X aktive" ---
    for line in lines:
        m = re.search(r'(\d+)\s*biler?\s*inn', line)
        if m:
            result["market_active_similar"] = int(m.group(1))
            break
        m = re.search(r'(\d+)\s*aktive', line.lower())
        if m:
            result["market_active_similar"] = int(m.group(1))
            break

    # --- Sold: "X biler ut" or "X solgt" ---
    for line in lines:
        m = re.search(r'(\d+)\s*biler?\s*ut', line)
        if m:
            result["market_sold_90d"] = int(m.group(1))
            break

    # --- Comp stats (from AFTER estimate section — informational only) ---
    # Keywords may be on the same line as the price, or the price may be on the next line
    comp_lines = lines[estimate_end:]
    for i, line in enumerate(comp_lines):
        ll = line.lower()
        for keyword, field in [('median', 'comp_median'), ('billigste', 'comp_cheapest'), ('dyreste', 'comp_most_expensive')]:
            if keyword in ll:
                # Try price on same line first
                p = _extract_price_from_text(line)
                # If not found, try the next line
                if not p and i + 1 < len(comp_lines):
                    p = _extract_price_from_text(comp_lines[i + 1])
                if p:
                    result[field] = p

    result["market_comps"] = []

    if result.get("market_anchor_price"):
        logger.debug(
            "Pristips innerText parsed: price=%s, low=%s, high=%s, days=%s",
            result.get("market_anchor_price"),
            result.get("market_anchor_low"),
            result.get("market_anchor_high"),
            result.get("market_days_to_sell"),
        )

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


def _save_debug_if_enabled(regnr: str, km: int, session: dict[str, Any], extraction_failed: bool = False) -> None:
    """Save debug artifacts only if toggle is on OR extraction failed."""
    if extraction_failed:
        # Always save on failure
        _save_debug_artifacts(regnr, km, session)
        return
    try:
        import yaml as _yaml
        cfg_path = Path(__file__).parent.parent.parent / "config" / "params.yaml"
        with open(cfg_path) as f:
            cfg = _yaml.safe_load(f)
        if cfg.get("pristips_debug", {}).get("save_artifacts", False):
            _save_debug_artifacts(regnr, km, session)
    except Exception:
        pass  # Don't fail on config read errors


def _save_debug_artifacts(regnr: str, km: int, session: dict[str, Any]) -> None:
    """Save comprehensive debug artifacts when extraction fails.

    Saves: screenshot, HTML, innerText, XHR responses, and a summary.
    Same artifacts as debug_pristips.py for easy comparison.
    """
    try:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_dir = DEBUG_DIR / f"pristips_{regnr}_{km}_{ts}"
        out_dir.mkdir(parents=True, exist_ok=True)

        inner_text = session.get("inner_text", "")
        html = session.get("html", "")
        xhr = session.get("captured_responses", [])

        # Screenshot
        screenshot = session.get("screenshot_bytes")
        if screenshot:
            (out_dir / "screenshot.png").write_bytes(screenshot)

        # HTML
        (out_dir / "page.html").write_text(html, encoding="utf-8")

        # Inner text
        (out_dir / "inner_text.txt").write_text(inner_text, encoding="utf-8")

        # XHR responses
        xhr_safe = [{"url": r.get("url", ""), "status": r.get("status"), "body": r.get("body")} for r in xhr]
        (out_dir / "xhr_responses.json").write_text(
            json.dumps(xhr_safe, indent=2, default=str, ensure_ascii=False),
            encoding="utf-8",
        )

        # Summary
        summary = [
            f"Pristips extraction FAILED for {regnr} / {km} km",
            f"Timestamp: {ts}",
            f"Final URL: {session.get('final_url', '')}",
            f"Login required: {session.get('login_required', False)}",
            f"Regnr filled: {session.get('regnr_filled', False)}",
            f"KM refilled: {session.get('km_refilled', False)}",
            f"InnerText length: {len(inner_text)} chars",
            f"HTML length: {len(html)} chars",
            f"XHR responses captured: {len(xhr)}",
            f"Contains 'ca.': {'ca.' in inner_text}",
            f"Contains 'mellom': {'mellom' in inner_text}",
            f"Contains 'Prisestimat': {'Prisestimat' in inner_text}",
            f"Contains 'Selg den selv': {'Selg den selv' in inner_text}",
            f"Error: {session.get('error')}",
        ]
        (out_dir / "summary.txt").write_text("\n".join(summary), encoding="utf-8")

        logger.info("Debug artifacts saved to %s", out_dir)
    except Exception as e:
        logger.debug("Failed to save debug artifacts: %s", e)


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
        "valuation_mode": "api_activity_only",
        "anchor_confidence": "NONE",
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }

    return result if any(v is not None for k, v in result.items() if k.startswith("market_") and k != "market_comps") else None


# ---------------------------------------------------------------------------
# Main entry points
# ---------------------------------------------------------------------------

def get_pristips(registration_number: str, km: int, headless: bool = True) -> dict[str, Any] | None:
    """Fetch FINN Pristips data.

    Uses the exact same flow as scripts/run_live_get_pristips.py:
    1. Load cookies, run browser session
    2. Extract price from /api/ads/price/valuation XHR (authoritative)
    3. Extract market activity from other XHR endpoints
    4. innerText fallback for price
    5. Script JSON fallback for price
    6. API fallback (no price, market activity only)
    """
    reg = registration_number.strip().upper().replace(" ", "")
    km_int = int(km)

    logger.debug(f"[PRISTIPS] ENTER get_pristips regnr={reg} km={km_int}")

    # --- Step 1: Browser extraction (same as run_live_get_pristips.py) ---
    browser_result = None

    if COOKIE_FILE.exists():
        try:
            from playwright.sync_api import sync_playwright  # noqa: F401
            cookies = json.loads(COOKIE_FILE.read_text())
            logger.debug(f"[PRISTIPS] Loaded {len(cookies)} cookies, running browser session...")

            session = run_pristips_browser_session(reg, km_int, cookies, headless=headless)

            if session.get("error"):
                logger.debug(f"[PRISTIPS] Browser session error: {session['error']}")
            elif session.get("login_required"):
                logger.debug("[PRISTIPS] Login required — cookies expired")
            else:
                captured = session["captured_responses"]
                inner_text = session["inner_text"]
                logger.debug(f"[PRISTIPS] Session OK: {len(captured)} XHR, {len(inner_text)} chars innerText")

                # Conditionally save artifacts (toggle-controlled)
                _save_debug_if_enabled(reg, km_int, session, extraction_failed=False)

                result: dict[str, Any] = {}

                # Strategy 1: valuation XHR (authoritative price)
                valuation = _extract_valuation_from_xhr(captured)
                if valuation:
                    result.update(valuation)
                    logger.debug(f"[PRISTIPS] Valuation XHR: price={result['market_anchor_price']}")

                # Strategy 2: market activity from other XHR
                activity = _extract_market_activity_from_xhr(captured)
                if activity:
                    for k, v in activity.items():
                        if v is not None and result.get(k) is None:
                            result[k] = v

                # Strategy 3: innerText fallback for price
                if not result.get("market_anchor_price"):
                    it_data = _parse_pristips_innertext(inner_text)
                    if it_data:
                        for k, v in it_data.items():
                            if v is not None and result.get(k) is None:
                                result[k] = v
                        if result.get("market_anchor_price"):
                            logger.debug(f"[PRISTIPS] innerText fallback: price={result['market_anchor_price']}")

                # Strategy 4: script JSON fallback for price
                if not result.get("market_anchor_price"):
                    script_data = _parse_pristips_script_json(session["html"])
                    if script_data:
                        for k, v in script_data.items():
                            if v is not None and result.get(k) is None:
                                result[k] = v

                # Merge extra innerText data even if we got price from XHR
                if result.get("market_anchor_price") and valuation:
                    it_data = _parse_pristips_innertext(inner_text)
                    if it_data:
                        for k, v in it_data.items():
                            if v is not None and result.get(k) is None:
                                result[k] = v

                if result.get("market_anchor_price"):
                    result["registration_number"] = reg
                    result["km"] = km_int
                    result["fetched_at"] = datetime.now(timezone.utc).isoformat()
                    result["source"] = "finn_pristips_browser"
                    result["valuation_mode"] = "browser_xhr" if valuation else "browser_innertext"
                    result["anchor_confidence"] = "HIGH" if valuation else "MEDIUM"
                    logger.debug(f"[PRISTIPS] RETURNING browser: price={result['market_anchor_price']}, low={result.get('market_anchor_low')}, high={result.get('market_anchor_high')}")
                    return result

                # Partial browser data (no price but has activity)
                if result and any(v is not None for k, v in result.items() if k.startswith("market_")):
                    browser_result = result
                    browser_result["valuation_mode"] = "browser_activity_only"
                    browser_result["anchor_confidence"] = "NONE"

        except ImportError:
            logger.debug("[PRISTIPS] Playwright not installed")
        except Exception as e:
            logger.debug(f"[PRISTIPS] Browser exception: {e}")
    else:
        logger.debug(f"[PRISTIPS] No cookies file at {COOKIE_FILE}")

    # --- Step 2: API fallback ---
    logger.debug("[PRISTIPS] Browser got no price, trying API fallback...")
    api_result = _api_fallback(reg, km_int)
    if api_result:
        # Merge partial browser data
        if browser_result:
            for k, v in browser_result.items():
                if v is not None and api_result.get(k) is None:
                    api_result[k] = v
        logger.debug(f"[PRISTIPS] RETURNING API fallback: price={api_result.get('market_anchor_price')}")
        return api_result

    logger.debug("[PRISTIPS] RETURNING None")
    return None


def get_pristips_cached(
    registration_number: str,
    km: int,
    force_refresh: bool = False,
) -> dict[str, Any] | None:
    """Cache wrapper: reuse cached result within 7 days."""
    # Round km to nearest 10k so cache keys are stable across runs
    km = max(round(int(km) / 10000) * 10000, 1000)
    logger.debug(f"[PRISTIPS] ENTER get_pristips_cached regnr={registration_number} km={km} force_refresh={force_refresh}")
    if not force_refresh:
        cached = get_cached_pristips(registration_number, int(km), max_age_days=7)
        if cached:
            logger.debug(f"[PRISTIPS] get_pristips_cached: CACHE HIT, price={cached.get('market_anchor_price')}")
            cached["source"] = "finn_pristips_cache"
            if "valuation_mode" not in cached:
                cached["valuation_mode"] = "cache"
            if "anchor_confidence" not in cached:
                cached["anchor_confidence"] = "HIGH" if cached.get("market_anchor_price") else "NONE"
            return cached
        logger.debug("[PRISTIPS] get_pristips_cached: cache miss")

    result = get_pristips(registration_number, int(km))
    if result:
        upsert_pristips_cache(registration_number, int(km), result)

    logger.debug(f"[PRISTIPS] get_pristips_cached RETURNING: price={result.get('market_anchor_price') if result else 'None'}")
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
    Logs instrumentation metrics after each run.
    """
    from src.engine.regnr_registry import get_reference_regnr

    batch_start = time.time()

    # Group by model/variant/year
    groups: dict[str, list[dict[str, Any]]] = {}
    for listing in listings:
        key = f"{listing.get('make', '')}_{listing.get('model', '')}_{listing.get('variant', 'unknown')}_{listing.get('year', 0)}"
        groups.setdefault(key, []).append(listing)

    results: dict[str, dict[str, Any]] = {}
    lookups_attempted = 0
    lookups_success = 0
    listings_covered = 0
    groups_no_regnr = 0

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
            groups_no_regnr += 1
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
            lookups_attempted += 1
            pristips = get_pristips_cached(regnr, km_bucket)

            if pristips:
                lookups_success += 1
                for l in bucket_listings:
                    results[l["listing_id"]] = pristips
                    listings_covered += 1

            # Rate limit between browser lookups
            time.sleep(3)

    elapsed = time.time() - batch_start
    avg_per_lookup = elapsed / max(lookups_attempted, 1)

    logger.info(
        "BATCH METRICS: %d total listings | %d unique batch keys | "
        "%d covered by batch | %d lookups attempted | %d lookups success (%.0f%%) | "
        "%d groups no regnr | %.1fs total | %.1fs per lookup",
        len(listings),
        len(groups),
        listings_covered,
        lookups_attempted,
        lookups_success,
        (lookups_success / max(lookups_attempted, 1)) * 100,
        groups_no_regnr,
        elapsed,
        avg_per_lookup,
    )

    return results
