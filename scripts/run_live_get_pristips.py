"""Run the REAL get_pristips() path with full print() tracing.

This calls the actual production code path — not a separate debug flow.
All temporary print() statements inside pristips.py will fire.

Usage:
    python scripts/run_live_get_pristips.py EC60771 72123
    python scripts/run_live_get_pristips.py EC60771 72123 --visible
    python scripts/run_live_get_pristips.py EC60771 72123 --both
"""

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.engine.pristips import get_pristips


def main():
    parser = argparse.ArgumentParser(description="Run REAL get_pristips() with tracing")
    parser.add_argument("regnr", help="Registration number (e.g. EC60771)")
    parser.add_argument("km", type=int, help="Mileage in km (e.g. 72123)")
    parser.add_argument("--visible", action="store_true", help="Run with headless=False")
    parser.add_argument("--both", action="store_true", help="Run BOTH headless and visible, compare results")
    args = parser.parse_args()

    if args.both:
        print("=" * 70)
        print("RUN 1: HEADLESS (headless=True)")
        print("=" * 70)
        result_headless = get_pristips(args.regnr, args.km, headless=True)
        print()
        print("=" * 70)
        print("RUN 2: VISIBLE (headless=False)")
        print("=" * 70)
        result_visible = get_pristips(args.regnr, args.km, headless=False)
        print()

        print("=" * 70)
        print("COMPARISON")
        print("=" * 70)
        h_price = result_headless.get("market_anchor_price") if result_headless else None
        v_price = result_visible.get("market_anchor_price") if result_visible else None
        h_source = result_headless.get("source") if result_headless else None
        v_source = result_visible.get("source") if result_visible else None
        print(f"  Headless: price={h_price}, source={h_source}")
        print(f"  Visible:  price={v_price}, source={v_source}")
        if h_price and v_price:
            print(f"  MATCH: {'YES' if h_price == v_price else 'NO'}")
        elif h_price:
            print("  Headless got price, visible did NOT")
        elif v_price:
            print("  Visible got price, headless did NOT  <-- THIS IS THE BUG")
        else:
            print("  NEITHER got a price")
    else:
        headless = not args.visible
        print("=" * 70)
        print(f"RUNNING: get_pristips('{args.regnr}', {args.km}, headless={headless})")
        print("=" * 70)
        result = get_pristips(args.regnr, args.km, headless=headless)

    # Print final result
    if not args.both:
        print()
        print("=" * 70)
        print("FINAL RESULT")
        print("=" * 70)
        if result:
            print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        else:
            print("None")


if __name__ == "__main__":
    main()
