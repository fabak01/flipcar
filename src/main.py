"""Main orchestrator: scrape -> Pristips (smart batch) -> AI -> comps -> underwrite -> alert."""

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.db.supabase_client import (
    clear_caches,
    get_cached_text_analysis,
    get_previous_run,
    log_scrape_run,
    upsert_analysis,
    upsert_raw_listing,
    upsert_text_analysis_cache,
)
from src.engine.comps import find_comps
from src.engine.pristips import get_pristips_batch_smart, get_pristips_cached
from src.engine.regnr_registry import build_regnr_registry, get_reference_regnr, get_reference_regnr_with_confidence, save_registry
from src.engine.rep_estimator import estimate_repairs
from src.engine.text_analyzer import analyze_listing_text
from src.engine.underwriting import underwrite_deal
from src.output.formatter import write_csv, write_jsonl
from src.output.telegram_bot import format_deal_message, send_deal_alert_new, send_health_alert
from src.scraper.finn_scraper import flatten_results, load_config, scrape_all_models, scrape_model

CONFIG_DIR = PROJECT_ROOT / "config"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler(PROJECT_ROOT / "flipcar.log")],
)
logger = logging.getLogger(__name__)


def load_all_params() -> dict[str, Any]:
    with open(CONFIG_DIR / "params.yaml") as f:
        return yaml.safe_load(f)


def check_health(results: dict[str, list[dict[str, Any]]], params: dict[str, Any]) -> str:
    health_params = params["health"]
    total = sum(len(v) for v in results.values())
    per_model = {k: len(v) for k, v in results.items()}

    previous = get_previous_run()
    status = "OK"

    if previous:
        prev_total = previous.get("total_listings", 0)
        if prev_total > 0 and total < prev_total * health_params["min_listings_pct_of_previous"]:
            status = "ALARM"
            send_health_alert(f"Listings dropped: {total} vs previous {prev_total} ({total/prev_total:.0%})")

        prev_per_model = previous.get("listings_per_model", {})
        for model_key, count in per_model.items():
            prev_count = prev_per_model.get(model_key, 0)
            if prev_count > 0:
                change = abs(count - prev_count) / prev_count
                if change > health_params["max_listing_count_change_pct"]:
                    if status != "ALARM":
                        status = "WARNING"
                    send_health_alert(f"Model {model_key}: count changed {prev_count} -> {count} ({change:.0%})")

    return status


def run_daily(model_filter: str | None = None, dry_run: bool = False, do_clear_cache: bool = False) -> None:
    load_dotenv()
    params = load_all_params()

    if do_clear_cache:
        logger.info("Clearing all caches...")
        clear_caches()

    logger.info("=== FlipCar Daily Run Start ===")

    # 1. Scrape all listings
    config = load_config()

    if model_filter:
        filter_lower = model_filter.lower()
        filtered_models = [m for m in config["models"] if filter_lower in f"{m['make']} {m['model']}".lower()]
        if not filtered_models:
            logger.error("No models matching filter '%s'", model_filter)
            return
        logger.info("Filtered to %d model(s): %s", len(filtered_models), [f"{m['make']} {m['model']}" for m in filtered_models])
        results: dict[str, list[dict[str, Any]]] = {}
        for m in filtered_models:
            key = f"{m['make']}_{m['model']}".lower().replace(' ', '_').replace('-', '').replace('.', '')
            listings = scrape_model(m["make"], m["model"], m.get("finn_query", f"{m['make']} {m['model']}"), config["params"], config.get("aliases", {}))
            results[key] = listings
    else:
        results = scrape_all_models(config)

    all_listings = flatten_results(results)
    logger.info("Scraped %d listings total", len(all_listings))

    # 2. Save raw data
    for listing in all_listings:
        upsert_raw_listing(listing)

    # 3. Build reference regnr registry
    registry = build_regnr_registry(all_listings)
    save_registry(registry, all_listings)
    logger.info("Registry: %d reference regnr", len(registry))

    # 4. Pristips for ALL listings (smart batching)
    logger.info("Fetching Pristips (smart batch)...")
    pristips_results = get_pristips_batch_smart(all_listings, registry)
    pristips_count = 0
    for listing in all_listings:
        lid = listing.get("listing_id", "")
        if lid in pristips_results:
            listing["pristips"] = pristips_results[lid]
            pristips_count += 1
        else:
            # Try individual lookup for listings not covered by batch
            regnr = listing.get("registration_number")
            if not regnr:
                regnr, regnr_confidence = get_reference_regnr_with_confidence(
                    registry, listing.get("make", ""), listing.get("model", ""),
                    listing.get("variant", "unknown"), listing.get("year", 0),
                )
                listing["regnr_source"] = "reference" if regnr else None
                listing["regnr_confidence"] = regnr_confidence
            else:
                listing["regnr_source"] = "listing"
                listing["regnr_confidence"] = "HIGH"

            # LOW confidence regnr → skip for production (WEAK_ANCHOR)
            if listing.get("regnr_confidence") == "LOW":
                listing["pristips"] = None
                listing["pristips_skip_reason"] = "WEAK_ANCHOR"
            elif regnr and (listing.get("km") or 0) > 0:
                listing["pristips"] = get_pristips_cached(regnr, int(listing["km"]))
                if listing["pristips"]:
                    pristips_count += 1
            else:
                listing["pristips"] = None

    logger.info("Pristips fetched for %d/%d listings", pristips_count, len(all_listings))

    # 5. AI analysis for all listings with text
    #    Cache keyed by listing_id + text hash to invalidate on text changes.
    import hashlib

    ai_count = 0
    ai_cache_hits = 0
    ai_cache_stale = 0
    for listing in all_listings:
        listing_id = listing.get("listing_id", "")
        listing_text = listing.get("listing_text", "")

        if len(listing_text) < 20:
            listing["ai_analysis"] = None
            continue

        # Deterministic text hash for cache invalidation
        text_hash = hashlib.sha256(listing_text.encode("utf-8")).hexdigest()[:16]

        cached_ai = get_cached_text_analysis(listing_id)
        if cached_ai:
            cached_hash = cached_ai.get("_text_hash", "")
            if cached_hash == text_hash:
                listing["ai_analysis"] = cached_ai
                ai_count += 1
                ai_cache_hits += 1
                continue
            else:
                ai_cache_stale += 1
                logger.debug("Stale AI cache for %s (text changed)", listing_id)

        ai = analyze_listing_text(
            listing_text,
            listing.get("make", ""),
            listing.get("model", ""),
            int(listing.get("year") or 0),
        )
        ai["_text_hash"] = text_hash
        listing["ai_analysis"] = ai
        upsert_text_analysis_cache(listing_id, ai)
        ai_count += 1

    logger.info("AI analysis for %d/%d listings (cache hits: %d, stale invalidated: %d)", ai_count, len(all_listings), ai_cache_hits, ai_cache_stale)

    # 6. Comps for all listings (diagnostics/sanity check only — NOT used as anchor)
    for listing in all_listings:
        comp_result = find_comps(listing, all_listings, params)
        listing["comp_result"] = comp_result

    # 7. Rep estimate for all listings
    for listing in all_listings:
        rep = estimate_repairs(listing, params=params)
        listing["rep_estimate"] = rep

    # 8. Full underwriting for listings with Pristips price (REQUIRED anchor)
    #    Comps alone are NOT sufficient for production underwriting.
    deals: list[dict[str, Any]] = []
    errors: list[str] = []
    pristips_missing_count = 0

    for listing in all_listings:
        pristips = listing.get("pristips") or {}

        # Production rule: Pristips price REQUIRED
        has_pristips_price = pristips.get("market_anchor_price") is not None

        if not has_pristips_price:
            pristips_missing_count += 1
            continue

        try:
            deal = underwrite_deal(listing, params)
            deals.append(deal)
        except Exception as e:
            errors.append(f"{listing.get('listing_id')}: {e}")
            logger.error("Underwriting failed for %s: %s", listing.get("listing_id"), e)

    # 9. Sort by profit
    deals.sort(key=lambda d: d.get("scenarios", {}).get("80pct", {}).get("profit_base", -999999), reverse=True)

    # 9a. Stage B: Exact re-verification for final candidates
    #     If a deal would trigger an alert (KONTAKT/KONTAKT forsiktig) AND was valued
    #     using a batch/reference regnr, re-verify with the listing's own regnr if available.
    exact_reverify_count = 0
    for deal in deals:
        c = deal.get("classification", {})
        l = deal.get("listing", {})
        if not c.get("send_telegram"):
            continue  # Only re-verify alert candidates
        if l.get("regnr_source") == "listing":
            continue  # Already used own regnr
        own_regnr = l.get("registration_number")
        own_km = l.get("km") or 0
        if own_regnr and own_km > 0:
            exact_pristips = get_pristips_cached(own_regnr, int(own_km), force_refresh=True)
            if exact_pristips and exact_pristips.get("market_anchor_price"):
                l["pristips"] = exact_pristips
                l["regnr_source"] = "listing_exact_reverify"
                l["regnr_confidence"] = "HIGH"
                exact_reverify_count += 1
                # Re-underwrite with exact data
                try:
                    deal.update(underwrite_deal(l, params))
                except Exception as e:
                    logger.error("Exact re-verify failed for %s: %s", l.get("listing_id"), e)

    if exact_reverify_count > 0:
        # Re-sort after re-verification
        deals.sort(key=lambda d: d.get("scenarios", {}).get("80pct", {}).get("profit_base", -999999), reverse=True)
        logger.info("Stage B: exact re-verified %d finalist candidates", exact_reverify_count)

    logger.info("Underwritten %d deals (%d errors, %d PRISTIPS_MISSING)", len(deals), len(errors), pristips_missing_count)

    # 9b. Pristips health check — alert if too many listings missing Pristips
    if len(all_listings) > 0:
        pristips_rate = pristips_count / len(all_listings)
        health_cfg = params.get("pristips_health", {})
        min_rate = health_cfg.get("min_success_rate", 0.3)
        if pristips_rate < min_rate:
            cookie_steps = (
                "Pristips-cookies trolig utloept.\n\n"
                "RECOVERY:\n"
                "1. cd ~/Projects/flipcar-clean\n"
                "2. source .venv/bin/activate\n"
                "3. python scripts/get_finn_cookies.py\n"
                "4. Logg inn paa FINN i nettleseren\n"
                "5. Test: python scripts/run_live_get_pristips.py EC60771 72123\n\n"
                f"Pristips success rate: {pristips_rate:.0%} ({pristips_count}/{len(all_listings)})\n"
                f"Listings uten Pristips: {pristips_missing_count}"
            )
            send_health_alert(cookie_steps)
            logger.warning("Pristips success rate %.0f%% below threshold %.0f%%", pristips_rate * 100, min_rate * 100)

    # 10. Build audit records for output
    audit_records = []
    for deal in deals:
        l = deal.get("listing", {})
        s80 = deal.get("scenarios", {}).get("80pct", {})
        market = deal.get("market", {})
        entry = deal.get("entry", {})
        adj = deal.get("adjustments", {})
        record = {
            # Listing metadata
            "listing_id": l.get("listing_id"),
            "listing_url": l.get("listing_url"),
            "make": l.get("make"),
            "model": l.get("model"),
            "variant": l.get("variant"),
            "year": l.get("year"),
            "km": l.get("km"),
            "listing_price_nok": l.get("price_nok"),
            "location": l.get("location_city"),
            "dq_score": l.get("dq_score"),
            "seller_type": l.get("seller_type"),
            "regnr_source": l.get("regnr_source"),
            "regnr_confidence": l.get("regnr_confidence"),
            # Classification
            "classification": deal.get("classification", {}).get("label", ""),
            "classification_reason": deal.get("classification", {}).get("reason", ""),
            "loan_recommendation": deal.get("classification", {}).get("loan_rec", ""),
            # Market anchor (Pristips)
            "market": market,
            # Adjustments (transparent breakdown)
            "adjustments": adj,
            # Exit values
            "exit": deal.get("exit", {}),
            # Entry values (separate from exit)
            "entry": entry,
            "assumed_entry_price": entry.get("assumed_entry_price"),
            "assumed_negotiation_discount": entry.get("total_discount"),
            # Repair costs
            "rep": deal.get("rep", {}),
            # Days to sell
            "days": deal.get("days", {}),
            # Fees
            "fees": deal.get("fees"),
            # Profit scenarios (cash, 60%, 80% LTV)
            "scenarios": deal.get("scenarios", {}),
            # MPP
            "mpp": deal.get("mpp"),
            "required_discount": deal.get("required_discount"),
            # Diagnostics
            "comps": deal.get("comps", {}),
            "soh_analysis": deal.get("soh", {}),
            "ai_analysis": deal.get("ai_analysis", {}),
        }
        audit_records.append(record)
        upsert_analysis(record)

    # 11. Telegram alerts (skip in dry_run)
    alerts_sent = 0
    if not dry_run:
        for deal in deals:
            if send_deal_alert_new(deal):
                c = deal.get("classification", {})
                if c.get("send_telegram"):
                    alerts_sent += 1
    else:
        # Print deals that WOULD be sent
        for deal in deals[:10]:
            c = deal.get("classification", {})
            l = deal.get("listing", {})
            s80 = deal.get("scenarios", {}).get("80pct", {})
            m = deal.get("market", {})
            print(f"\n{'='*60}")
            print(f"{c.get('emoji','')} {c.get('label','')} | {l.get('make','')} {l.get('model','')} {l.get('variant','')} {l.get('year','')}")
            print(f"  Pris: {l.get('price_nok', 0):,} kr | FMV: {m.get('anchor', 0):,} kr ({m.get('source', '?')})")
            print(f"  Comps: {deal.get('comps', {}).get('n_comps', 0)} (Tier {deal.get('comps', {}).get('tier', '?')})")
            print(f"  Pristips days: {m.get('days_to_sell', 'N/A')} | Active: {m.get('active_similar', 'N/A')} | Sold 90d: {m.get('sold_90d', 'N/A')}")
            print(f"  Exit bull/base/bear: {deal.get('exit', {}).get('bull', 0):,} / {deal.get('exit', {}).get('base', 0):,} / {deal.get('exit', {}).get('bear', 0):,}")
            print(f"  Profit 80% base: {s80.get('profit_base', 0):+,} | bear: {s80.get('profit_bear', 0):+,}")
            print(f"  MPP: {deal.get('mpp', 0):,} | Discount needed: {deal.get('required_discount', 0):.1%}")
            print(f"  Laan: {c.get('loan_rec', 'N/A')}")
            if c.get("send_telegram"):
                print(f"  >>> VILLE SENDT TELEGRAM <<<")
                print(format_deal_message(deal)[:500])

    # 12. Output files
    write_jsonl(audit_records, str(PROJECT_ROOT / "deals.jsonl"))
    write_csv(audit_records, str(PROJECT_ROOT / "deals.csv"))

    # 13. Health check
    health_status = check_health(results, params)
    per_model = {k: len(v) for k, v in results.items()}
    log_scrape_run(len(all_listings), per_model, errors, health_status)

    logger.info(
        "=== FlipCar Daily Run Complete === (%d scraped, %d underwritten, %d alerts, %d errors, %d pristips_missing)",
        len(all_listings), len(deals), alerts_sent, len(errors), pristips_missing_count,
    )


def run() -> None:
    """Backward-compatible entrypoint."""
    run_daily()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="FlipCar Deal Radar")
    parser.add_argument("--model", type=str, default=None, help="Filter to a single model (e.g. 'Tesla Model 3')")
    parser.add_argument("--dry-run", action="store_true", help="Print deals to terminal, don't send Telegram")
    parser.add_argument("--clear-cache", action="store_true", help="Clear all Supabase caches before running")
    args = parser.parse_args()
    run_daily(model_filter=args.model, dry_run=args.dry_run, do_clear_cache=args.clear_cache)
