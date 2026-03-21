"""Active diagnostic + extraction tool for FINN Pristips.

Uses the SAME browser automation as get_pristips() via run_pristips_browser_session(),
then performs additional diagnostic analysis and saves detailed artifacts.

Usage:
    python scripts/debug_pristips.py EC60771 72000
    python scripts/debug_pristips.py EC60771 72000 --visible
"""

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.engine.pristips import (
    COOKIE_FILE,
    run_pristips_browser_session,
    _parse_pristips_innertext,
    _extract_market_activity_from_xhr,
)

DEBUG_DIR = PROJECT_ROOT / "debug"

# Keys for deep JSON search (diagnostic only)
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
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = DEBUG_DIR / f"pristips_{regnr}_{km}_{ts}"
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"=== Pristips Debug: {regnr} / {km} km ===")
    print(f"Output: {out_dir}")
    print()

    if not COOKIE_FILE.exists():
        print("ERROR: No cookies file. Run: python scripts/get_finn_cookies.py")
        return

    cookies = json.loads(COOKIE_FILE.read_text())
    print(f"Loaded {len(cookies)} cookies")

    # --- Use the SAME browser session as get_pristips() ---
    print("\nRunning shared browser session (same as get_pristips)...")
    session = run_pristips_browser_session(regnr, km, cookies, headless=not visible)

    if session.get("error"):
        print(f"\nERROR: {session['error']}")
        return

    if session.get("login_required"):
        print("\n*** LOGIN REQUIRED - cookies expired! ***")
        print("Run: python scripts/get_finn_cookies.py")
        return

    inner_text = session["inner_text"]
    html = session["html"]
    captured = session["captured_responses"]

    print(f"\nSession results:")
    print(f"  Final URL: {session['final_url']}")
    print(f"  Regnr filled: {session['regnr_filled']}")
    print(f"  KM refilled on result page: {session['km_refilled']}")
    print(f"  InnerText length: {len(inner_text)} chars")
    print(f"  HTML length: {len(html)} chars")
    print(f"  XHR responses captured: {len(captured)}")
    print(f"  Contains 'ca.': {'ca.' in inner_text}")
    print(f"  Contains 'mellom': {'mellom' in inner_text}")

    # --- Save artifacts ---
    print("\nSaving artifacts...")

    # Screenshot
    screenshot = session.get("screenshot_bytes")
    if screenshot:
        (out_dir / "screenshot.png").write_bytes(screenshot)
        print("  screenshot.png")

    # HTML
    (out_dir / "page.html").write_text(html, encoding="utf-8")
    print(f"  page.html ({len(html)} bytes)")

    # Inner text
    (out_dir / "inner_text.txt").write_text(inner_text, encoding="utf-8")
    print(f"  inner_text.txt ({len(inner_text)} chars)")

    # XHR responses
    json_dir = out_dir / "json_responses"
    json_dir.mkdir(exist_ok=True)
    for i, resp in enumerate(captured):
        url = resp.get("url", "")
        safe_name = re.sub(r'[^a-zA-Z0-9_-]', '_', url.split("?")[0].split("/")[-1] or "root")
        fname = f"{i + 1:03d}_{safe_name}.json"
        (json_dir / fname).write_text(
            json.dumps(resp, indent=2, default=str, ensure_ascii=False),
            encoding="utf-8",
        )
    print(f"  json_responses/ ({len(captured)} responses)")

    # --- Now attempt extraction using the SAME parsers as get_pristips() ---
    print("\n" + "=" * 60)
    print("EXTRACTION (using same parsers as get_pristips)")
    print("=" * 60)

    # Strategy 1: innerText parser
    print("\n--- Strategy 1: _parse_pristips_innertext ---")
    result = _parse_pristips_innertext(inner_text)
    if result:
        for k, v in sorted(result.items()):
            if v is not None and k != "market_comps":
                print(f"  {k} = {v}")
        if result.get("market_anchor_price"):
            print(f"\n  *** PRICE FOUND: {result['market_anchor_price']:,} kr (innerText) ***")
    else:
        print("  No result from innerText parser")

    # Strategy 2: XHR market activity
    print("\n--- Strategy 2: _extract_market_activity_from_xhr ---")
    xhr_data = _extract_market_activity_from_xhr(captured)
    if xhr_data:
        for k, v in sorted(xhr_data.items()):
            if v is not None:
                print(f"  {k} = {v}")
    else:
        print("  No market activity from XHR")

    # --- Additional diagnostic: raw price search in XHR ---
    print("\n--- Diagnostic: deep price search in XHR (NOT used by get_pristips) ---")
    for i, resp in enumerate(captured):
        body = resp.get("body")
        url = resp.get("url", "")
        if not body:
            continue
        prices_found = _deep_search_prices(body)
        if prices_found:
            print(f"  Response {i + 1}: {url[:80]}")
            for key, val in prices_found.items():
                print(f"    {key} = {val}")

    # --- Diagnostic: innerText lines with prices ---
    print("\n--- Diagnostic: innerText lines with prices ---")
    lines = [l.strip() for l in inner_text.replace('\xa0', ' ').split('\n') if l.strip()]
    for i, line in enumerate(lines):
        if re.search(r'\d{2,3}\s\d{3}', line) or 'kr' in line.lower():
            print(f"  L{i}: {line[:120]}")

    # === Summary ===
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)

    found_price = result.get("market_anchor_price") if result else None
    summary_lines = []

    if found_price:
        msg = f"PRICE FOUND: {found_price:,} kr (innerText parser)"
        print(f"\n  *** {msg} ***")
        summary_lines.append(msg)
        if result:
            summary_lines.append(f"market_anchor_low: {result.get('market_anchor_low')}")
            summary_lines.append(f"market_anchor_high: {result.get('market_anchor_high')}")
    else:
        print("\n  *** PRICE NOT FOUND ***")
        print("\n  Next steps:")
        print("  1. Open screenshot.png - verify the price IS visible on the page")
        print("  2. Open inner_text.txt - search for the price number manually")
        print("  3. Check json_responses/ - look for valuation/price endpoints")
        summary_lines.append("PRICE NOT FOUND")

    summary_lines.append("")
    summary_lines.append(f"Final URL: {session['final_url']}")
    summary_lines.append(f"KM refilled: {session['km_refilled']}")
    summary_lines.append(f"XHR responses captured: {len(captured)}")
    summary_lines.append(f"InnerText length: {len(inner_text)} chars")
    summary_lines.append(f"HTML length: {len(html)} chars")
    summary_lines.append(f"Contains 'ca.': {'ca.' in inner_text}")
    summary_lines.append(f"Contains 'mellom': {'mellom' in inner_text}")

    (out_dir / "summary.txt").write_text("\n".join(summary_lines), encoding="utf-8")
    print(f"\n  All artifacts saved to: {out_dir}")


def _deep_search_prices(data, depth=0, path="") -> dict:
    """Recursively search JSON for price-related fields (diagnostic only)."""
    if depth > 8:
        return {}
    result = {}

    if isinstance(data, dict):
        for key, val in data.items():
            kl = key.lower()
            cur_path = f"{path}.{key}" if path else key

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

            for lo_name, hi_name in RANGE_KEYS:
                if kl == lo_name.lower() and isinstance(val, (int, float)) and val > 10000:
                    result["market_anchor_low"] = int(val)
                if kl == hi_name.lower() and isinstance(val, (int, float)) and val > 10000:
                    result["market_anchor_high"] = int(val)

            if kl in [k.lower() for k in DAYS_KEYS]:
                if isinstance(val, (int, float)) and 1 <= val <= 365:
                    result["market_days_to_sell"] = int(val)

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
