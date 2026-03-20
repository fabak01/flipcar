"""Active diagnostic + extraction tool for FINN Pristips.

Opens the Pristips page with saved cookies, attempts to extract the price,
and saves comprehensive debug artifacts if extraction fails.

Usage:
    python scripts/debug_pristips.py EC60771 72000
    python scripts/debug_pristips.py EC60771 72000 --visible
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

COOKIE_FILE = PROJECT_ROOT / ".finn_cookies.json"
DEBUG_DIR = PROJECT_ROOT / "debug"
PRISTIPS_URL = "https://www.finn.no/mobility/insights/price-valuation"

# Keys we search for in JSON responses
PRICE_KEYS = [
    "priceEstimate", "estimatedPrice", "pricePrediction", "predictedPrice",
    "valuationPrice", "marketPrice", "price", "value", "amount",
    "priceEstimation", "estimatePrice", "sellerPrice", "dealerPrice",
    "privateSalePrice", "valuationAmount",
]
RANGE_KEYS = [
    ("priceLow", "priceHigh"), ("priceMin", "priceMax"),
    ("lowEstimate", "highEstimate"), ("intervalLow", "intervalHigh"),
    ("confidenceIntervalLow", "confidenceIntervalHigh"),
    ("low", "high"), ("min", "max"), ("from", "to"),
]
DAYS_KEYS = [
    "daysToSell", "medianDaysToSell", "publishingTimeMedian",
    "estimatedDaysToSell", "medianDays", "publishingTime",
]
CONTEXT_WORDS = [
    "prisestimat", "estimert", "verdi", "pristips", "market",
    "pris", "selg", "kjop", "annonse", "biler",
]


def run(regnr: str, km: int, visible: bool = False):
    from playwright.sync_api import sync_playwright

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = DEBUG_DIR / f"pristips_{regnr}_{km}_{ts}"
    out_dir.mkdir(parents=True, exist_ok=True)
    json_dir = out_dir / "json_responses"
    json_dir.mkdir(exist_ok=True)

    print(f"=== Pristips Debug: {regnr} / {km} km ===")
    print(f"Output: {out_dir}")
    print()

    if not COOKIE_FILE.exists():
        print("ERROR: No cookies file. Run: python scripts/get_finn_cookies.py")
        return

    cookies = json.loads(COOKIE_FILE.read_text())
    print(f"Loaded {len(cookies)} cookies")

    captured: list[dict] = []
    network_urls: list[str] = []

    def on_response(response):
        url = response.url
        status = response.status
        ct = response.headers.get("content-type", "")
        network_urls.append(f"{status} {ct[:40]:40s} {url}")

        if "json" in ct:
            try:
                body = response.json()
            except Exception:
                body = None
            entry = {"url": url, "status": status, "body": body}
            captured.append(entry)

            # Save each JSON response
            safe_name = re.sub(r'[^a-zA-Z0-9_-]', '_', url.split("?")[0].split("/")[-1] or "root")
            idx = len(captured)
            fname = f"{idx:03d}_{safe_name}.json"
            (json_dir / fname).write_text(
                json.dumps({"url": url, "status": status, "body": body}, indent=2, default=str, ensure_ascii=False),
                encoding="utf-8",
            )

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not visible)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 900},
        )
        context.add_cookies(cookies)
        page = context.new_page()
        page.on("response", on_response)

        # --- Navigate ---
        print("Navigating to Pristips...")
        page.goto(PRISTIPS_URL, wait_until="domcontentloaded", timeout=20000)
        page.wait_for_timeout(2000)

        # Cookie banner
        try:
            page.click("button:has-text('Godta alle')", timeout=2000)
            print("Dismissed cookie banner")
        except Exception:
            pass

        # --- Fill regnr ---
        print(f"Filling regnr: {regnr}")
        regnr_filled = False
        for sel in [
            'input#r0', 'input[name="registration-number"]',
            'input[name*="registration"]', 'input[placeholder*="registrering"]',
            'input[placeholder*="regnr"]', 'input[aria-label*="Registreringsnummer"]',
        ]:
            try:
                el = page.locator(sel).first
                if el.is_visible(timeout=500):
                    el.fill(regnr)
                    regnr_filled = True
                    print(f"  Used selector: {sel}")
                    break
            except Exception:
                continue
        if not regnr_filled:
            # Last resort: first visible text input
            try:
                el = page.locator('input[type="text"]').first
                el.fill(regnr)
                regnr_filled = True
                print("  Used fallback: input[type=text]")
            except Exception:
                pass
        if not regnr_filled:
            print("  FAILED to find regnr input!")

        page.wait_for_timeout(500)

        # --- Fill km ---
        print(f"Filling km: {km}")
        km_filled = False
        for sel in [
            'input#mileage', 'input[name="mileage"]', 'input[name*="mileage"]',
            'input[placeholder*="km"]', 'input[placeholder*="Kilometerstand"]',
            'input[aria-label*="Kilometerstand"]', 'input[aria-label*="km"]',
        ]:
            try:
                el = page.locator(sel).first
                if el.is_visible(timeout=500):
                    el.fill(str(km))
                    km_filled = True
                    print(f"  Used selector: {sel}")
                    break
            except Exception:
                continue
        if not km_filled:
            print("  No km field found on this page (may appear after submit)")

        page.wait_for_timeout(500)

        # --- Submit ---
        print("Submitting...")
        submitted = False
        for sel in [
            'button:has-text("Sjekk bil")', 'button:has-text("Sjekk")',
            'button:has-text("Hent")', 'button:has-text("Beregn")',
            'button[type="submit"]',
        ]:
            try:
                btn = page.locator(sel).first
                if btn.is_visible(timeout=500):
                    btn.click()
                    submitted = True
                    print(f"  Clicked: {sel}")
                    break
            except Exception:
                continue
        if not submitted:
            page.keyboard.press("Enter")
            print("  Pressed Enter (fallback)")

        # --- Wait for results ---
        print("Waiting for results (15s networkidle + 5s extra)...")
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
        page.wait_for_timeout(5000)

        # Check login redirect
        cur_url = page.url
        print(f"Current URL: {cur_url}")
        body_text = page.evaluate("document.body?.innerText || ''")
        if "login" in cur_url or "E-postadresse" in body_text or "Logg inn" in body_text:
            print("\n*** LOGIN REQUIRED - cookies expired! ***")
            print("Run: python scripts/get_finn_cookies.py")
            browser.close()
            return

        # --- Second km fill if needed ---
        for sel in ['input#mileage', 'input[name="mileage"]', 'input[placeholder*="km"]']:
            try:
                el = page.locator(sel).first
                if el.is_visible(timeout=500):
                    cur_val = el.input_value()
                    if not cur_val or cur_val == "0":
                        el.fill(str(km))
                        print(f"Filled km on result page: {sel}")
                        for btn_sel in ['button:has-text("Oppdater")', 'button:has-text("Beregn")']:
                            try:
                                btn = page.locator(btn_sel).first
                                if btn.is_visible(timeout=500):
                                    btn.click()
                                    print(f"Clicked update: {btn_sel}")
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

        # --- Save artifacts ---
        print("\nSaving artifacts...")

        # Screenshot
        page.screenshot(path=str(out_dir / "screenshot.png"), full_page=True)
        print(f"  screenshot.png")

        # HTML
        html = page.content()
        (out_dir / "page.html").write_text(html, encoding="utf-8")
        print(f"  page.html ({len(html)} bytes)")

        # Inner text
        inner_text = page.evaluate("document.body?.innerText || ''")
        (out_dir / "inner_text.txt").write_text(inner_text, encoding="utf-8")
        print(f"  inner_text.txt ({len(inner_text)} chars)")

        # Network URLs
        (out_dir / "network_urls.txt").write_text("\n".join(network_urls), encoding="utf-8")
        print(f"  network_urls.txt ({len(network_urls)} requests)")

        # JSON responses
        print(f"  json_responses/ ({len(captured)} responses)")

        browser.close()

    # --- Now attempt extraction ---
    print("\n" + "=" * 60)
    print("EXTRACTION ATTEMPTS")
    print("=" * 60)

    found_price = None
    found_source = None

    # === Attempt 1: XHR responses ===
    print("\n--- Strategy 1: XHR JSON responses ---")
    for i, resp in enumerate(captured):
        body = resp.get("body")
        url = resp.get("url", "")
        if not body:
            continue

        prices_found = _deep_search_prices(body)
        if prices_found:
            print(f"  Response {i+1}: {url[:80]}")
            for key, val in prices_found.items():
                print(f"    {key} = {val}")
                if key == "market_anchor_price" and not found_price:
                    found_price = val
                    found_source = f"XHR response {i+1}: {url[:80]}"

    if not found_price:
        print("  No price found in XHR responses")

    # === Attempt 2: innerText ===
    print("\n--- Strategy 2: innerText parsing ---")
    lines = [l.strip() for l in inner_text.split('\n') if l.strip()]

    # Show all lines containing price-like patterns
    price_lines = []
    for i, line in enumerate(lines):
        if re.search(r'\d{2,3}[\s\xa0]\d{3}', line) or 'kr' in line.lower():
            price_lines.append((i, line))
    if price_lines:
        print(f"  Lines with prices ({len(price_lines)} total):")
        for idx, line in price_lines[:20]:
            print(f"    L{idx}: {line[:120]}")

    # Show lines with context words
    context_lines = []
    for i, line in enumerate(lines):
        ll = line.lower()
        for w in CONTEXT_WORDS:
            if w in ll:
                context_lines.append((i, line, w))
                break
    if context_lines:
        print(f"\n  Lines with context words ({len(context_lines)}):")
        for idx, line, word in context_lines[:20]:
            print(f"    L{idx} [{word}]: {line[:120]}")

    # Try automatic extraction
    for i, line in enumerate(lines):
        ll = line.lower()
        if any(k in ll for k in ['prisestimat', 'estimert pris', 'estimert verdi', 'selg den selv']):
            for j in range(max(0, i - 2), min(len(lines), i + 5)):
                m = re.search(r'(\d{2,3}[\s\xa0]\d{3})', lines[j])
                if m:
                    val = int(re.sub(r'[\s\xa0]', '', m.group(1)))
                    if 10000 < val < 10000000 and not found_price:
                        found_price = val
                        found_source = f"innerText L{j}: '{lines[j][:80]}'"
                        print(f"\n  ** FOUND via context: {val} at L{j}")
                    break

    if not found_price:
        # Try "ca. NNN NNN kr"
        for line in lines:
            m = re.search(r'ca\.?\s*([\d\s\xa0]+\d)\s*kr', line)
            if m:
                val = int(re.sub(r'[\s\xa0]', '', m.group(1)))
                if 10000 < val < 10000000:
                    found_price = val
                    found_source = f"innerText 'ca. X kr': '{line[:80]}'"
                    print(f"\n  ** FOUND via 'ca. X kr': {val}")
                    break

    # === Attempt 3: script tag JSON ===
    print("\n--- Strategy 3: Inline script JSON ---")
    import base64
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    script_count = 0
    for script in soup.find_all("script"):
        text = script.string or ""
        if not text or len(text) < 50:
            continue
        script_count += 1

        # base64
        if len(text) > 1000 and text.strip().startswith("eyJ"):
            try:
                decoded = base64.b64decode(text.strip()).decode("utf-8")
                data = json.loads(decoded)
                prices = _deep_search_prices(data)
                if prices:
                    print(f"  base64 script ({len(text)} chars): {prices}")
                    if prices.get("market_anchor_price") and not found_price:
                        found_price = prices["market_anchor_price"]
                        found_source = f"base64 script tag ({len(text)} chars)"
            except Exception:
                pass

        # Inline JSON with price keys
        for key in PRICE_KEYS:
            m = re.search(rf'"{key}"\s*:\s*(\d+)', text)
            if m:
                val = int(m.group(1))
                if 10000 < val < 10000000:
                    print(f"  Inline JSON: {key} = {val}")
                    if not found_price:
                        found_price = val
                        found_source = f"inline JSON key '{key}' in script tag"

    print(f"  Checked {script_count} script tags")

    # === Summary ===
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)

    summary_lines = []

    if found_price:
        msg = f"PRICE FOUND: {found_price:,} kr"
        print(f"\n  *** {msg} ***")
        print(f"  Source: {found_source}")
        summary_lines.append(msg)
        summary_lines.append(f"Source: {found_source}")
    else:
        print("\n  *** PRICE NOT FOUND ***")
        print("\n  Next steps:")
        print("  1. Open screenshot.png - verify the price IS visible on the page")
        print("  2. Open inner_text.txt - search for the price number manually")
        print("  3. Check json_responses/ - look for valuation/price endpoints")
        print("  4. Open page.html in a browser - inspect the DOM structure")
        summary_lines.append("PRICE NOT FOUND")
        summary_lines.append("")
        summary_lines.append("Inspect order:")
        summary_lines.append("1. screenshot.png - is the price visible?")
        summary_lines.append("2. inner_text.txt - search for the number")
        summary_lines.append("3. json_responses/ - API data")
        summary_lines.append("4. page.html - DOM structure")

    summary_lines.append("")
    summary_lines.append(f"XHR responses captured: {len(captured)}")
    summary_lines.append(f"Network requests: {len(network_urls)}")
    summary_lines.append(f"InnerText length: {len(inner_text)} chars")
    summary_lines.append(f"HTML length: {len(html)} chars")
    summary_lines.append(f"URL after submit: {cur_url}")

    (out_dir / "summary.txt").write_text("\n".join(summary_lines), encoding="utf-8")
    print(f"\n  All artifacts saved to: {out_dir}")


def _deep_search_prices(data, depth=0, path="") -> dict:
    """Recursively search JSON for price-related fields."""
    if depth > 8:
        return {}
    result = {}

    if isinstance(data, dict):
        for key, val in data.items():
            kl = key.lower()
            cur_path = f"{path}.{key}" if path else key

            # Direct price keys
            if kl in [k.lower() for k in PRICE_KEYS]:
                if isinstance(val, (int, float)) and 10000 < val < 10000000:
                    result["market_anchor_price"] = int(val)
                    result["_price_path"] = cur_path
                elif isinstance(val, dict):
                    for sub_key in ["amount", "value", "price"]:
                        sv = val.get(sub_key)
                        if isinstance(sv, (int, float)) and 10000 < sv < 10000000:
                            result["market_anchor_price"] = int(sv)
                            result["_price_path"] = f"{cur_path}.{sub_key}"

            # Range keys
            for lo_name, hi_name in RANGE_KEYS:
                if kl == lo_name.lower() and isinstance(val, (int, float)) and val > 10000:
                    result["market_anchor_low"] = int(val)
                if kl == hi_name.lower() and isinstance(val, (int, float)) and val > 10000:
                    result["market_anchor_high"] = int(val)

            # Days keys
            if kl in [k.lower() for k in DAYS_KEYS]:
                if isinstance(val, (int, float)) and 1 <= val <= 365:
                    result["market_days_to_sell"] = int(val)

            # Recurse
            if isinstance(val, (dict, list)):
                sub = _deep_search_prices(val, depth + 1, cur_path)
                if sub.get("market_anchor_price") and not result.get("market_anchor_price"):
                    result.update(sub)

    elif isinstance(data, list):
        for i, item in enumerate(data):
            if isinstance(item, (dict, list)):
                sub = _deep_search_prices(item, depth + 1, f"{path}[{i}]")
                if sub.get("market_anchor_price") and not result.get("market_anchor_price"):
                    result.update(sub)

    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Debug FINN Pristips extraction")
    parser.add_argument("regnr", help="Registration number (e.g. EC60771)")
    parser.add_argument("km", type=int, help="Mileage in km (e.g. 72000)")
    parser.add_argument("--visible", action="store_true", help="Show browser window")
    args = parser.parse_args()
    run(args.regnr, args.km, args.visible)
