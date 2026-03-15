"""Browser-based FINN Pristips extraction using Playwright.

Requires FINN login. Session is managed via:
  - FINN_EMAIL + FINN environment variables for auto-login
  - Or pre-saved cookies in Supabase / local file

The browser extracts the visible price estimate from the rendered page,
plus interval, days-to-sell, active/sold counts, and comp data.
"""

import json
import logging
import os
import re
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Browser binary paths (Playwright-managed)
_BROWSER_CANDIDATES = [
    Path.home() / ".cache/ms-playwright/chromium-1194/chrome-linux/chrome",
    Path.home() / ".cache/ms-playwright/chromium-1148/chrome-linux/chrome",
    Path.home() / ".cache/ms-playwright/chromium_headless_shell-1194/chrome-linux/headless_shell",
]

PRISTIPS_URL = "https://www.finn.no/mobility/insights/price-valuation"
COOKIE_FILE = Path(__file__).parent.parent.parent / ".finn_cookies.json"


def _find_browser() -> str | None:
    """Find an installed Chromium binary."""
    for p in _BROWSER_CANDIDATES:
        if p.exists():
            return str(p)
    # Try generic path pattern
    cache = Path.home() / ".cache/ms-playwright"
    if cache.exists():
        for d in sorted(cache.iterdir(), reverse=True):
            if d.name.startswith("chromium-"):
                chrome = d / "chrome-linux" / "chrome"
                if chrome.exists():
                    return str(chrome)
    return None


def _get_proxy_config() -> dict[str, str] | None:
    """Get proxy config from environment."""
    proxy_url = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    if not proxy_url:
        return None
    parsed = urllib.parse.urlparse(proxy_url)
    if not parsed.hostname:
        return None
    config: dict[str, str] = {
        "server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}",
    }
    if parsed.username:
        config["username"] = urllib.parse.unquote(parsed.username)
    if parsed.password:
        config["password"] = urllib.parse.unquote(parsed.password)
    return config


def _parse_price(text: str) -> int | None:
    """Parse Norwegian price string like '244 000 kr' or 'ca. 244 000 kr' to int."""
    if not text:
        return None
    # Remove "ca.", "kr", spaces, non-breaking spaces
    cleaned = re.sub(r'[^\d]', '', text.replace('\xa0', ' '))
    if cleaned and cleaned.isdigit():
        val = int(cleaned)
        if 10000 < val < 10000000:  # sanity: 10k - 10M
            return val
    return None


def _parse_days(text: str) -> int | None:
    """Parse days-to-sell string like 'ca. 15 dager'."""
    m = re.search(r'(\d+)\s*dager', text)
    return int(m.group(1)) if m else None


def _parse_count(text: str) -> int | None:
    """Parse count string like '43 biler' or '177'."""
    m = re.search(r'(\d[\d\s]*\d|\d+)', text)
    if m:
        return int(m.group(1).replace(' ', ''))
    return None


def _extract_results_from_page(page: Any) -> dict[str, Any]:
    """Extract all visible Pristips data from rendered page.

    Looks for price estimate, interval, days-to-sell, active/sold counts,
    and comparable vehicle distribution data.
    """
    result: dict[str, Any] = {
        "market_anchor_price": None,
        "market_anchor_low": None,
        "market_anchor_high": None,
        "market_days_to_sell": None,
        "market_active_similar": None,
        "market_sold_90d": None,
        "market_new_last_30d": None,
        "market_sold_last_30d": None,
        "comp_count": None,
        "comp_median": None,
        "comp_cheapest": None,
        "comp_most_expensive": None,
        "market_comps": [],
    }

    try:
        body_text = page.evaluate("document.body?.innerText || ''")
    except Exception:
        body_text = ""

    if not body_text or len(body_text) < 50:
        logger.warning("Pristips page has no content")
        return result

    lines = [l.strip() for l in body_text.split('\n') if l.strip()]

    # Strategy 1: look for price estimate pattern
    # "Prisestimat" / "Estimert pris" / "Verdi" followed by a price
    for i, line in enumerate(lines):
        ll = line.lower()
        if any(k in ll for k in ['prisestimat', 'estimert pris', 'estimert verdi']):
            # The price is usually on the next line or same line
            for j in range(i, min(i + 3, len(lines))):
                price = _parse_price(lines[j])
                if price:
                    result["market_anchor_price"] = price
                    break
            if result["market_anchor_price"]:
                break

    # Strategy 2: look for "ca. NNN NNN kr" pattern (the main estimate)
    if not result["market_anchor_price"]:
        for line in lines:
            m = re.search(r'ca\.?\s*(\d[\d\s]*\d)\s*kr', line)
            if m:
                val = int(m.group(1).replace(' ', ''))
                if 10000 < val < 10000000:
                    result["market_anchor_price"] = val
                    break

    # Look for interval (e.g. "231 000 – 256 000 kr" or "fra X til Y")
    for line in lines:
        m = re.search(r'(\d[\d\s]+\d)\s*[-–]\s*(\d[\d\s]+\d)\s*kr', line)
        if m:
            low = int(m.group(1).replace(' ', ''))
            high = int(m.group(2).replace(' ', ''))
            if 10000 < low < high < 10000000:
                result["market_anchor_low"] = low
                result["market_anchor_high"] = high
                break

    # Days to sell
    for line in lines:
        if 'dager' in line.lower():
            days = _parse_days(line)
            if days and 1 <= days <= 365:
                result["market_days_to_sell"] = days
                break

    # Active similar / sold counts
    for i, line in enumerate(lines):
        ll = line.lower()
        if 'aktive' in ll or 'biler inn' in ll:
            count = _parse_count(line)
            if count:
                result["market_active_similar"] = count
        if 'solgt' in ll or 'biler ut' in ll or 'avpublisert' in ll:
            count = _parse_count(line)
            if count:
                if '90' in ll or 'siste 3' in ll:
                    result["market_sold_90d"] = count
                elif '30' in ll or 'siste måned' in ll:
                    result["market_sold_last_30d"] = count
                else:
                    result["market_sold_90d"] = count
        if 'nye siste' in ll or 'nye annonser' in ll:
            count = _parse_count(line)
            if count:
                result["market_new_last_30d"] = count

    # Distribution stats (median, cheapest, most expensive, count)
    for line in lines:
        ll = line.lower()
        if 'median' in ll:
            result["comp_median"] = _parse_price(line)
        if 'billigste' in ll:
            result["comp_cheapest"] = _parse_price(line)
        if 'dyreste' in ll:
            result["comp_most_expensive"] = _parse_price(line)
        m = re.search(r'(\d+)\s*biler', line)
        if m and not result["comp_count"]:
            result["comp_count"] = int(m.group(1))

    return result


def _save_cookies(page: Any) -> None:
    """Save browser cookies to file for reuse."""
    try:
        cookies = page.context.cookies()
        with open(COOKIE_FILE, "w") as f:
            json.dump(cookies, f)
        logger.info("Saved %d cookies to %s", len(cookies), COOKIE_FILE)
    except Exception as e:
        logger.warning("Failed to save cookies: %s", e)


def _load_cookies(context: Any) -> bool:
    """Load previously saved cookies into browser context."""
    if not COOKIE_FILE.exists():
        return False
    try:
        with open(COOKIE_FILE) as f:
            cookies = json.load(f)
        context.add_cookies(cookies)
        logger.info("Loaded %d cookies from %s", len(cookies), COOKIE_FILE)
        return True
    except Exception as e:
        logger.warning("Failed to load cookies: %s", e)
        return False


def _do_login(page: Any) -> bool:
    """Perform FINN login if credentials are available.

    Uses FINN_EMAIL env var. FINN uses passwordless login (email code),
    so this requires either:
    1. Pre-saved session cookies (preferred)
    2. Manual login flow (not automatable without email access)
    """
    email = os.environ.get("FINN_EMAIL", "")
    if not email:
        logger.warning("No FINN_EMAIL configured, cannot login")
        return False

    # Check if we're on the login page
    try:
        body = page.evaluate("document.body?.innerText || ''")
        if 'E-postadresse' not in body and 'Logg inn' not in body:
            # Already logged in
            return True
    except Exception:
        pass

    logger.warning(
        "FINN requires login. Set FINN_EMAIL and save cookies manually, "
        "or provide pre-authenticated cookies in %s",
        COOKIE_FILE,
    )
    return False


def extract_pristips_browser(
    registration_number: str,
    km: int,
    timeout_ms: int = 30000,
) -> dict[str, Any] | None:
    """Extract Pristips data using Playwright browser.

    Returns structured dict with market_anchor_price, interval, etc.
    Returns None if extraction fails.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logger.error("Playwright not installed. Run: pip install playwright && playwright install chromium")
        return None

    browser_path = _find_browser()
    if not browser_path:
        logger.error("No Chromium browser found. Run: playwright install chromium")
        return None

    proxy = _get_proxy_config()
    reg = registration_number.strip().upper().replace(" ", "")

    captured_responses: list[dict] = []

    def on_response(response):
        url = response.url
        ct = response.headers.get("content-type", "")
        if "json" in ct and "price-valuation" in url:
            try:
                body = response.json()
            except Exception:
                body = None
            captured_responses.append({"url": url, "status": response.status, "body": body})

    result = None

    try:
        with sync_playwright() as pw:
            launch_args = {
                "headless": True,
                "args": ["--no-sandbox", "--disable-gpu", "--ignore-certificate-errors"],
                "executable_path": browser_path,
            }
            if proxy:
                launch_args["proxy"] = proxy

            browser = pw.chromium.launch(**launch_args)
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
                viewport={"width": 1280, "height": 900},
                ignore_https_errors=True,
            )

            # Load saved cookies
            _load_cookies(context)

            page = context.new_page()
            page.on("response", on_response)

            # Navigate to Pristips
            page.goto(PRISTIPS_URL, wait_until="domcontentloaded", timeout=timeout_ms)
            page.wait_for_timeout(2000)

            # Accept cookies banner if present
            try:
                page.click("button:has-text('Godta alle')", timeout=2000)
            except Exception:
                pass

            # Fill regnr and km
            regnr_input = page.query_selector("#r0")
            km_input = page.query_selector("#mileage")

            if not regnr_input or not km_input:
                logger.warning("Could not find input fields on Pristips page")
                browser.close()
                return None

            regnr_input.fill(reg)
            page.wait_for_timeout(300)
            km_input.fill(str(int(km)))
            page.wait_for_timeout(300)

            # Click submit
            try:
                page.get_by_text("Sjekk bil").click(timeout=3000)
            except Exception:
                # Fallback: press Enter
                km_input.press("Enter")

            # Wait for result or login redirect
            page.wait_for_timeout(5000)

            # Check if redirected to login
            body_text = page.evaluate("document.body?.innerText || ''")
            if 'E-postadresse' in body_text or 'Logg inn' in body_text:
                logger.info("FINN requires login, attempting...")
                if not _do_login(page):
                    logger.warning("Cannot login to FINN, browser extraction unavailable")
                    browser.close()
                    return None

            # Wait for results to render
            page.wait_for_timeout(8000)

            # Extract from rendered DOM
            result = _extract_results_from_page(page)

            # Also capture any XHR price data
            for resp in captured_responses:
                if resp["body"] and isinstance(resp["body"], dict):
                    # Look for price estimate in API responses
                    body = resp["body"]
                    if "priceEstimate" in body or "estimatedPrice" in body or "price_estimate" in body:
                        price = body.get("priceEstimate") or body.get("estimatedPrice") or body.get("price_estimate")
                        if isinstance(price, (int, float)) and price > 10000:
                            result["market_anchor_price"] = int(price)

            result["raw_xhr"] = captured_responses
            result["source"] = "finn_pristips_browser"
            result["fetched_at"] = datetime.now(timezone.utc).isoformat()

            # Save cookies for future use
            _save_cookies(page)

            browser.close()

    except Exception as e:
        logger.error("Playwright browser extraction failed: %s", e)
        return None

    if result and result.get("market_anchor_price"):
        logger.info(
            "Browser extracted: price=%s, low=%s, high=%s, days=%s",
            result["market_anchor_price"],
            result.get("market_anchor_low"),
            result.get("market_anchor_high"),
            result.get("market_days_to_sell"),
        )
    else:
        logger.info("Browser did not extract a price estimate (login required?)")

    return result


def smoke_test(regnr: str = "EK45405", km: int = 96843) -> None:
    """Run a quick smoke test of the browser extraction."""
    logging.basicConfig(level=logging.INFO)
    print(f"Testing Pristips browser extraction: regnr={regnr}, km={km}")

    result = extract_pristips_browser(regnr, km)
    if result:
        print(f"\nResults:")
        print(f"  market_anchor_price: {result.get('market_anchor_price')}")
        print(f"  market_anchor_low:   {result.get('market_anchor_low')}")
        print(f"  market_anchor_high:  {result.get('market_anchor_high')}")
        print(f"  days_to_sell:        {result.get('market_days_to_sell')}")
        print(f"  active_similar:      {result.get('market_active_similar')}")
        print(f"  sold_90d:            {result.get('market_sold_90d')}")
        print(f"  source:              {result.get('source')}")
        print(f"  XHR captured:        {len(result.get('raw_xhr', []))}")
    else:
        print("  Browser extraction returned None")

    print("\n>>> Done")


if __name__ == "__main__":
    smoke_test()
