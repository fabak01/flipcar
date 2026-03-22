"""Run the REAL Pristips extraction pipeline with full print() tracing.

Calls the lower-level functions directly (run_pristips_browser_session,
_extract_valuation_from_xhr, etc.) so it works regardless of which
version of get_pristips() is installed locally.

Usage:
    python scripts/run_live_get_pristips.py EC60771 72123
    python scripts/run_live_get_pristips.py EC60771 72123 --visible
    python scripts/run_live_get_pristips.py EC60771 72123 --both
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.engine.pristips import (
    COOKIE_FILE,
    run_pristips_browser_session,
    _extract_valuation_from_xhr,
    _extract_market_activity_from_xhr,
    _parse_pristips_innertext,
    _parse_pristips_script_json,
    _api_fallback,
    _save_debug_artifacts,
)

DEBUG_DIR = PROJECT_ROOT / "debug"


def run_pipeline(regnr: str, km: int, headless: bool) -> dict | None:
    """Execute the full Pristips pipeline with print tracing at every step."""
    reg = regnr.strip().upper().replace(" ", "")
    km_int = int(km)
    mode = "HEADLESS" if headless else "VISIBLE"

    print(f"[{mode}] === ENTER pipeline regnr={reg} km={km_int} headless={headless} ===")

    # --- Step 1: Load cookies ---
    if not COOKIE_FILE.exists():
        print(f"[{mode}] NO COOKIE FILE at {COOKIE_FILE}")
        print(f"[{mode}] Skipping browser, going to API fallback")
        return _do_api_fallback(reg, km_int, mode)

    cookies = json.loads(COOKIE_FILE.read_text())
    print(f"[{mode}] Loaded {len(cookies)} cookies from {COOKIE_FILE}")

    # --- Step 2: Run browser session ---
    print(f"[{mode}] Calling run_pristips_browser_session(headless={headless})...")
    session = run_pristips_browser_session(reg, km_int, cookies, headless=headless)

    if session.get("error"):
        print(f"[{mode}] SESSION ERROR: {session['error']}")
        return _do_api_fallback(reg, km_int, mode)

    if session.get("login_required"):
        print(f"[{mode}] LOGIN REQUIRED — cookies expired!")
        print(f"[{mode}] Run: python scripts/get_finn_cookies.py")
        return _do_api_fallback(reg, km_int, mode)

    inner_text = session["inner_text"]
    captured = session["captured_responses"]

    print(f"[{mode}] Session OK:")
    print(f"[{mode}]   final_url    = {session['final_url']}")
    print(f"[{mode}]   regnr_filled = {session['regnr_filled']}")
    print(f"[{mode}]   km_refilled  = {session['km_refilled']}")
    print(f"[{mode}]   innerText    = {len(inner_text)} chars")
    print(f"[{mode}]   html         = {len(session['html'])} chars")
    print(f"[{mode}]   XHR captured = {len(captured)} responses")
    print(f"[{mode}]   has 'ca.'    = {'ca.' in inner_text}")
    print(f"[{mode}]   has 'mellom' = {'mellom' in inner_text}")

    # --- Step 3: Print ALL captured XHR URLs ---
    print(f"[{mode}] --- ALL CAPTURED XHR URLs ---")
    valuation_count = 0
    for i, r in enumerate(captured):
        url = r.get("url", "")
        is_val = "/api/ads/price/valuation" in url
        if is_val:
            valuation_count += 1
        marker = " *** VALUATION ***" if is_val else ""
        print(f"[{mode}]   #{i:02d}: {url[:160]}{marker}")
    print(f"[{mode}] --- Valuation URLs found: {valuation_count} ---")

    # --- Always save artifacts ---
    _save_debug_artifacts(reg, km_int, session)
    print(f"[{mode}] Debug artifacts saved")

    result: dict = {}

    # --- Step 4: STRATEGY 1 — valuation XHR ---
    print(f"[{mode}] STRATEGY 1: _extract_valuation_from_xhr ({len(captured)} responses)")
    valuation = _extract_valuation_from_xhr(captured)
    if valuation:
        result.update(valuation)
        print(f"[{mode}] STRATEGY 1 SUCCESS:")
        print(f"[{mode}]   market_anchor_price = {result['market_anchor_price']}")
        print(f"[{mode}]   market_anchor_low   = {result.get('market_anchor_low')}")
        print(f"[{mode}]   market_anchor_high  = {result.get('market_anchor_high')}")
    else:
        print(f"[{mode}] STRATEGY 1 FAILED: no /api/ads/price/valuation response matched")

    # --- Step 5: STRATEGY 2 — XHR market activity ---
    print(f"[{mode}] STRATEGY 2: _extract_market_activity_from_xhr")
    activity = _extract_market_activity_from_xhr(captured)
    if activity:
        for k, v in activity.items():
            if v is not None and result.get(k) is None:
                result[k] = v
        print(f"[{mode}] STRATEGY 2: {sorted(activity.keys())}")
    else:
        print(f"[{mode}] STRATEGY 2: no market activity data")

    # --- Step 6: STRATEGY 3 — innerText fallback for price ---
    if not result.get("market_anchor_price"):
        print(f"[{mode}] STRATEGY 3: trying innerText for price")
        it_data = _parse_pristips_innertext(inner_text)
        if it_data:
            for k, v in it_data.items():
                if v is not None and result.get(k) is None:
                    result[k] = v
            if result.get("market_anchor_price"):
                print(f"[{mode}] STRATEGY 3 SUCCESS: price={result['market_anchor_price']}")
            else:
                print(f"[{mode}] STRATEGY 3: parsed but no price. keys={sorted(k for k, v in it_data.items() if v)}")
        else:
            print(f"[{mode}] STRATEGY 3: innerText parser returned None")
    else:
        # Still merge extra innerText data (comp stats, days)
        it_data = _parse_pristips_innertext(inner_text)
        if it_data:
            for k, v in it_data.items():
                if v is not None and result.get(k) is None:
                    result[k] = v

    # --- Step 7: STRATEGY 4 — script JSON fallback for price ---
    if not result.get("market_anchor_price"):
        print(f"[{mode}] STRATEGY 4: trying script JSON for price")
        script_data = _parse_pristips_script_json(session["html"])
        if script_data:
            for k, v in script_data.items():
                if v is not None and result.get(k) is None:
                    result[k] = v
            if result.get("market_anchor_price"):
                print(f"[{mode}] STRATEGY 4 SUCCESS: price={result.get('market_anchor_price')}")

    # --- Step 8: Check if we got a price ---
    if result.get("market_anchor_price"):
        result["registration_number"] = reg
        result["km"] = km_int
        result["fetched_at"] = datetime.now(timezone.utc).isoformat()
        result["source"] = "finn_pristips_browser"
        print(f"[{mode}] PIPELINE SUCCESS: price={result['market_anchor_price']}, low={result.get('market_anchor_low')}, high={result.get('market_anchor_high')}")
        return result

    # --- Step 9: API fallback ---
    print(f"[{mode}] Browser extraction got no price. Trying API fallback...")
    api_result = _do_api_fallback(reg, km_int, mode)
    if api_result and result:
        # Merge partial browser data into API result
        for k, v in result.items():
            if v is not None and api_result.get(k) is None:
                api_result[k] = v
    return api_result


def _do_api_fallback(reg: str, km_int: int, mode: str) -> dict | None:
    print(f"[{mode}] Calling _api_fallback({reg}, {km_int})...")
    api_result = _api_fallback(reg, km_int)
    if api_result:
        print(f"[{mode}] API fallback returned: days={api_result.get('market_days_to_sell')}, active={api_result.get('market_active_similar')}, price={api_result.get('market_anchor_price')}")
    else:
        print(f"[{mode}] API fallback returned None")
    return api_result


def main():
    parser = argparse.ArgumentParser(description="Run REAL Pristips pipeline with full tracing")
    parser.add_argument("regnr", help="Registration number (e.g. EC60771)")
    parser.add_argument("km", type=int, help="Mileage in km (e.g. 72123)")
    parser.add_argument("--visible", action="store_true", help="Run with headless=False")
    parser.add_argument("--both", action="store_true", help="Run BOTH headless and visible, compare")
    args = parser.parse_args()

    if args.both:
        print("=" * 70)
        print("RUN 1: HEADLESS")
        print("=" * 70)
        result_headless = run_pipeline(args.regnr, args.km, headless=True)
        print()
        print("=" * 70)
        print("RUN 2: VISIBLE")
        print("=" * 70)
        result_visible = run_pipeline(args.regnr, args.km, headless=False)
        print()

        print("=" * 70)
        print("COMPARISON")
        print("=" * 70)
        h_price = result_headless.get("market_anchor_price") if result_headless else None
        v_price = result_visible.get("market_anchor_price") if result_visible else None
        h_src = result_headless.get("source") if result_headless else None
        v_src = result_visible.get("source") if result_visible else None
        h_xhr = "unknown"
        v_xhr = "unknown"
        print(f"  Headless: price={h_price}, source={h_src}")
        print(f"  Visible:  price={v_price}, source={v_src}")
        if h_price and v_price:
            print(f"  MATCH: {'YES' if h_price == v_price else 'NO — prices differ!'}")
        elif h_price:
            print("  Headless got price, visible did NOT")
        elif v_price:
            print("  *** BUG: Visible got price, headless did NOT ***")
        else:
            print("  NEITHER got a price")

        print()
        print("=" * 70)
        print("HEADLESS RESULT")
        print("=" * 70)
        print(json.dumps(result_headless, indent=2, ensure_ascii=False, default=str) if result_headless else "None")
        print()
        print("=" * 70)
        print("VISIBLE RESULT")
        print("=" * 70)
        print(json.dumps(result_visible, indent=2, ensure_ascii=False, default=str) if result_visible else "None")
    else:
        headless = not args.visible
        print("=" * 70)
        print(f"RUNNING: pipeline('{args.regnr}', {args.km}, headless={headless})")
        print("=" * 70)
        result = run_pipeline(args.regnr, args.km, headless=headless)
        print()
        print("=" * 70)
        print("FINAL RESULT")
        print("=" * 70)
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str) if result else "None")


if __name__ == "__main__":
    main()
