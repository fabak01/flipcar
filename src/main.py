"""Main orchestrator: scrape -> Pristips -> AI -> comps -> underwrite -> alert.

CLI modes:
  --smoke      : 3 listings, no Telegram, compact table
  --dry-run    : all listings, no Telegram, write CSV/JSONL
  --live       : all listings, Telegram enabled
  --limit N    : limit to N listings after scraping
"""

import argparse
import json
import logging
import os
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
    mark_telegram_sent,
    upsert_analysis,
    upsert_raw_listing,
    upsert_text_analysis_cache,
)
from src.engine.comps import find_comps
from src.engine.pristips import get_pristips_batch_smart
from src.engine.regnr_registry import build_regnr_registry, get_reference_regnr, save_registry
from src.engine.rep_estimator import estimate_repairs
from src.engine.text_analyzer import analyze_listing_text
from src.engine.underwriting import underwrite_deal
from src.output.formatter import write_csv, write_jsonl
from src.output.telegram_bot import format_deal_message, send_deal_alert_new, send_health_alert
from src.scraper.finn_scraper import (
    enrich_listing_texts,
    flatten_results,
    load_config,
    scrape_all_models,
    scrape_model,
)

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


# ── Health check ──────────────────────────────────────────────────

def run_health_check() -> dict[str, str]:
    """Startup health check. Returns dict of component -> status."""
    results: dict[str, str] = {}

    # Pristips cookies
    cookie_file = PROJECT_ROOT / ".finn_cookies.json"
    if cookie_file.exists():
        results["FINN cookies"] = "OK"
    else:
        results["FINN cookies"] = "MISSING — run: python scripts/get_finn_cookies.py"

    # AI
    if os.getenv("OPENAI_API_KEY"):
        results["AI (gpt-4o-mini)"] = "OK"
    else:
        results["AI (gpt-4o-mini)"] = "MISSING OPENAI_API_KEY — AI will be skipped"

    # Telegram
    if os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID"):
        results["Telegram"] = "OK"
    else:
        results["Telegram"] = "MISSING — set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID"

    # Supabase — mirror exact key names used by supabase_client._get_client()
    supabase_key = os.getenv("SUPABASE_SERVICE_KEY") or os.getenv("SUPABASE_ANON_KEY")
    if os.getenv("SUPABASE_URL") and supabase_key:
        results["Supabase"] = "OK"
    else:
        results["Supabase"] = "MISSING — set SUPABASE_URL and SUPABASE_SERVICE_KEY (or SUPABASE_ANON_KEY)"

    # Pristips browser
    results["Pristips"] = "OK (browser-based)"

    return results


def print_health_check(results: dict[str, str]) -> None:
    print("\n=== FlipCar Health Check ===")
    for component, status in results.items():
        print(f"  {component:20s} {status}")
    print()


# ── Listing count health ──────────────────────────────────────────

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


# ── Main run ──────────────────────────────────────────────────────

def run_daily(
    model_filter: str | None = None,
    mode: str = "dry-run",
    limit: int | None = None,
    do_clear_cache: bool = False,
) -> None:
    load_dotenv()
    params = load_all_params()

    # Health check
    health = run_health_check()
    print_health_check(health)

    # --live requires Telegram
    telegram_enabled = mode == "live"
    if telegram_enabled and "MISSING" in health.get("Telegram", ""):
        logger.error("--live requires Telegram. Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.")
        return

    if do_clear_cache:
        logger.info("Clearing all caches...")
        clear_caches()

    # Smoke mode = limit 3
    if mode == "smoke":
        limit = limit or 3

    logger.info("=== FlipCar Run Start (mode=%s, limit=%s) ===", mode, limit)

    # 1. Scrape
    config = load_config()

    # In smoke mode pass the limit so each model stops scraping early
    scrape_max = limit if mode == "smoke" else None

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
            listings = scrape_model(m["make"], m["model"], m.get("finn_query", f"{m['make']} {m['model']}"), config["params"], config.get("aliases", {}), max_listings=scrape_max)
            results[key] = listings
    else:
        results = scrape_all_models(config, max_total_listings=scrape_max)

    all_listings = flatten_results(results)
    logger.info("Scraped %d listings total", len(all_listings))

    # Apply --limit BEFORE expensive downstream steps
    if limit and limit > 0 and len(all_listings) > limit:
        logger.info("Applying --limit %d (from %d listings)", limit, len(all_listings))
        all_listings = all_listings[:limit]

    # Enrich short listing texts from individual detail pages
    short_text_count = sum(1 for l in all_listings if len(l.get("listing_text", "") or "") < 100)
    if short_text_count > 0:
        logger.info("Fetching full description for %d listings with short text...", short_text_count)
        n_enriched = enrich_listing_texts(all_listings)
        logger.info("Enriched %d/%d listings with full description text", n_enriched, short_text_count)

    # 2. Save raw data
    for listing in all_listings:
        upsert_raw_listing(listing)

    # 3. Build reference regnr registry
    registry = build_regnr_registry(all_listings)
    save_registry(registry, all_listings)
    logger.info("Registry: %d reference regnr", len(registry))

    # 4. Pristips for ALL listings (smart batching)
    # Prefilter: skip listings that can never be deals to avoid expensive browser lookups
    PRICE_MIN, PRICE_MAX, YEAR_MIN, KM_MAX = 30_000, 800_000, 2012, 300_000
    prefilter_skipped = 0
    for listing in all_listings:
        price = listing.get("price_nok") or 0
        year = listing.get("year") or 0
        km_val = listing.get("km") or 0
        if price < PRICE_MIN or price > PRICE_MAX or year < YEAR_MIN or km_val > KM_MAX:
            listing["pristips"] = None
            listing["pristips_skip_reason"] = "PREFILTER"
            prefilter_skipped += 1
    prefilter_kept = len(all_listings) - prefilter_skipped
    logger.info(
        "Pristips prefilter: %d/%d listings pass (price %d-%d, year>=%d, km<%d), %d skipped",
        prefilter_kept, len(all_listings), PRICE_MIN, PRICE_MAX, YEAR_MIN, KM_MAX, prefilter_skipped,
    )

    logger.info("Fetching Pristips (smart batch)...")
    # Only pass prefilter-passing listings to the batch function
    eligible_listings = [l for l in all_listings if l.get("pristips_skip_reason") != "PREFILTER"]
    pristips_results = get_pristips_batch_smart(eligible_listings, registry)
    pristips_count = 0
    for listing in all_listings:
        if listing.get("pristips_skip_reason") == "PREFILTER":
            continue
        lid = listing.get("listing_id", "")
        if lid in pristips_results:
            listing["pristips"] = pristips_results[lid]
            pristips_count += 1
        else:
            listing["pristips"] = None
            listing.setdefault("pristips_skip_reason", "NOT_IN_BATCH")

    logger.info("Pristips fetched for %d/%d listings", pristips_count, len(all_listings))

    # 5. AI analysis (resilient — never blocks classification)
    import hashlib
    ai_count = 0
    ai_cache_hits = 0
    ai_cache_stale = 0
    for listing in all_listings:
        listing_id = listing.get("listing_id", "")
        listing_text = listing.get("listing_text", "")

        if len(listing_text) < 20:
            listing["ai_analysis"] = analyze_listing_text("", "", "", 0)  # returns short_text status
            continue

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
            seller_type=listing.get("seller_type", ""),
        )
        ai["_text_hash"] = text_hash
        listing["ai_analysis"] = ai
        upsert_text_analysis_cache(listing_id, ai)
        ai_count += 1

    logger.info("AI analysis for %d/%d listings (cache hits: %d, stale: %d)", ai_count, len(all_listings), ai_cache_hits, ai_cache_stale)

    # 6. Comps (diagnostics only)
    for listing in all_listings:
        comp_result = find_comps(listing, all_listings, params)
        listing["comp_result"] = comp_result

    # 7. Rep estimate
    for listing in all_listings:
        rep = estimate_repairs(listing, params=params)
        listing["rep_estimate"] = rep

    # 8. Price sanity gate
    price_skipped = 0
    for listing in all_listings:
        ptype = listing.get("price_parse_type", "sale_price")
        if ptype in ("monthly_price", "leasing_price"):
            listing["skip_reason"] = listing.get("skip_reason") or f"{ptype}_detected"
            price_skipped += 1

    if price_skipped > 0:
        logger.info("Price sanity: %d skipped (monthly/leasing)", price_skipped)

    # 9. Underwriting
    deals: list[dict[str, Any]] = []
    errors: list[str] = []
    pristips_missing_count = 0
    price_skip_count = 0

    for listing in all_listings:
        if listing.get("skip_reason") and listing.get("price_parse_type") in ("monthly_price", "leasing_price", "unknown"):
            price_skip_count += 1
            continue

        pristips = listing.get("pristips") or {}
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

    # Sort by spread
    deals.sort(key=lambda d: d.get("spread_ask_pct", 0), reverse=True)

    logger.info("Underwritten %d deals (%d errors, %d PRISTIPS_MISSING, %d price_skipped)",
                 len(deals), len(errors), pristips_missing_count, price_skip_count)

    # 10. Pristips health check
    if len(all_listings) > 0:
        pristips_rate = pristips_count / len(all_listings)
        health_cfg = params.get("pristips_health", {})
        min_rate = health_cfg.get("min_success_rate", 0.3)
        if pristips_rate < min_rate:
            cookie_steps = (
                "Pristips-cookies trolig utloept.\n\n"
                "RECOVERY:\n"
                "1. python scripts/get_finn_cookies.py\n"
                "2. Logg inn paa FINN\n"
                f"\nPristips success rate: {pristips_rate:.0%} ({pristips_count}/{len(all_listings)})"
            )
            send_health_alert(cookie_steps)
            logger.warning("Pristips success rate %.0f%% below threshold %.0f%%", pristips_rate * 100, min_rate * 100)

    # 11. Build audit records
    audit_records = []
    for deal in deals:
        l = deal.get("listing", {})
        m = deal.get("market", {})
        c = deal.get("classification", {})
        record = {
            "listing_id": l.get("listing_id"),
            "listing_url": l.get("listing_url"),
            "make": l.get("make"),
            "model": l.get("model"),
            "variant": l.get("variant"),
            "year": l.get("year"),
            "km": l.get("km"),
            "seller_type": l.get("seller_type"),
            "location_city": l.get("location_city"),
            # Core output
            "asking_price": deal.get("asking_price"),
            "market_anchor_price": m.get("anchor"),
            "market_anchor_low": m.get("low"),
            "market_anchor_high": m.get("high"),
            "adjusted_market_value": deal.get("adjusted_market_value"),
            "spread_ask_abs": deal.get("spread_ask_abs"),
            "spread_ask_pct": deal.get("spread_ask_pct"),
            "realistic_bid_price": deal.get("realistic_bid_price"),
            "spread_bid_abs": deal.get("spread_bid_abs"),
            "spread_bid_pct": deal.get("spread_bid_pct"),
            "adj_positive": deal.get("adj_positive"),
            "adj_positive_raw": deal.get("adj_positive_raw"),
            "adj_positive_capped": deal.get("adj_positive_capped"),
            "adj_negative": deal.get("adj_negative"),
            "repair_buffer": deal.get("repair_buffer"),
            # Classification + confidence
            "classification_label": c.get("label"),
            "execution_gate": c.get("execution_gate"),
            "hard_red_flag": deal.get("hard_red_flag"),
            "hard_red_flag_details": deal.get("hard_red_flag_details"),
            "confidence": deal.get("confidence"),
            "high_text_dependency": deal.get("high_text_dependency"),
            # Listing text length (for diagnosing ai_status=short_text)
            "listing_text_len": len((deal.get("listing") or {}).get("listing_text", "") or ""),
            # AI
            "ai_status": deal.get("ai_status"),
            "ai_summary_short": deal.get("ai_summary_short"),
            "ai_positive_signals": deal.get("ai_positive_signals"),
            "ai_negative_signals": deal.get("ai_negative_signals"),
            "ai_missing_info": deal.get("ai_missing_info"),
            "seller_motivation_score": deal.get("seller_motivation_score"),
            # Status
            "soh_status": deal.get("soh_status"),
            "eu_status": deal.get("eu_status"),
            "skip_reason": deal.get("skip_reason"),
            "explanation": deal.get("explanation"),
            # Detail (for JSONL)
            "market": m,
            "adjustments_detail": deal.get("adjustments_detail"),
            "bid_detail": deal.get("bid_detail"),
            "rep_detail": deal.get("rep_detail"),
            "comps": deal.get("comps"),
            "classification": c,
        }
        audit_records.append(record)
        upsert_analysis(record)

    # 12. Telegram / console output
    alerts_sent = 0
    if telegram_enabled:
        for deal in deals:
            if send_deal_alert_new(deal):
                gate = deal.get("classification", {}).get("execution_gate", "")
                if gate in ("SEND", "SEND_NO_AI"):
                    alerts_sent += 1
                    listing_id = deal.get("listing", {}).get("listing_id")
                    if listing_id:
                        mark_telegram_sent(listing_id)
    else:
        # Console output
        for deal in deals[:20 if mode != "smoke" else 3]:
            c = deal.get("classification", {})
            l = deal.get("listing", {})
            label = c.get("label", "?")
            gate = c.get("execution_gate", "?")
            ask = deal.get("asking_price", 0)
            anchor = deal.get("market_anchor_price", 0)
            v_adj = deal.get("adjusted_market_value", 0)
            spread_ask = deal.get("spread_ask_pct", 0)
            spread_bid = deal.get("spread_bid_pct", 0)

            print(f"\n{'='*70}")
            print(f"[{label}] {l.get('make','')} {l.get('model','')} {l.get('variant','')} {l.get('year','')}")
            print(f"  Ask: {ask:>10,} | Pristips: {anchor:>10,} | V_adj: {v_adj:>10,}")
            print(f"  Spread ask: {spread_ask:>7.1%} | Spread bid: {spread_bid:>7.1%}")
            print(f"  Adj+: {deal.get('adj_positive',0):>+8,} | Adj-: {deal.get('adj_negative',0):>8,} | Rep: {deal.get('repair_buffer',0):>8,}")
            txt_len = len((deal.get("listing") or {}).get("listing_text", "") or "")
            print(f"  Gate: {gate} | AI: {deal.get('ai_status','?')} | text_len: {txt_len} | Red flag: {deal.get('hard_red_flag', False)}")

            expl = deal.get("explanation", "")
            if expl:
                for line in expl.split("\n")[-3:]:
                    if line.strip():
                        print(f"  {line}")

            if gate in ("SEND", "SEND_NO_AI"):
                print(f"  >>> WOULD SEND TELEGRAM <<<")

    # 13. Output files
    write_jsonl(audit_records, str(PROJECT_ROOT / "deals.jsonl"))
    write_csv(audit_records, str(PROJECT_ROOT / "deals.csv"))

    # 14. Health check
    health_status = check_health(results, params)
    per_model = {k: len(v) for k, v in results.items()}
    log_scrape_run(len(all_listings), per_model, errors, health_status)

    logger.info(
        "=== FlipCar Run Complete === (%d scraped, %d underwritten, %d alerts, %d errors, %d pristips_missing)",
        len(all_listings), len(deals), alerts_sent, len(errors), pristips_missing_count,
    )


def run() -> None:
    """Backward-compatible entrypoint."""
    run_daily()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="FlipCar Deal Radar — Pristips-first used car flip finder")
    parser.add_argument("--model", type=str, default=None, help="Filter to a single model (e.g. 'Tesla Model 3')")
    parser.add_argument("--smoke", action="store_true", help="Smoke test: 3 listings, no Telegram, compact output")
    parser.add_argument("--dry-run", action="store_true", help="Process all, no Telegram, write CSV/JSONL")
    parser.add_argument("--live", action="store_true", help="Full run with Telegram alerts enabled")
    parser.add_argument("--limit", type=int, default=None, help="Limit to N listings after scraping")
    parser.add_argument("--clear-cache", action="store_true", help="Clear all Supabase caches before running")
    args = parser.parse_args()

    if args.smoke:
        mode = "smoke"
    elif args.live:
        mode = "live"
    else:
        mode = "dry-run"

    run_daily(model_filter=args.model, mode=mode, limit=args.limit, do_clear_cache=args.clear_cache)
