"""Main orchestrator: scrape, analyze, classify, notify."""

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.scraper.finn_scraper import flatten_results, load_config, scrape_all_models
from src.scraper.svv_lookup import enrich_listing
from src.engine.comps import find_comps
from src.engine.fmv import calculate_fmv
from src.engine.adjustments import apply_adjustments, detect_adjustments
from src.engine.rep_estimator import estimate_repairs
from src.engine.days_to_sell import estimate_days
from src.engine.carry import calculate_all_scenarios
from src.engine.profit import calculate_profit
from src.engine.mpp import calculate_mpp
from src.engine.classifier import classify_deal
from src.output.formatter import build_audit_record, write_csv, write_jsonl
from src.output.telegram_bot import send_deal_alert, send_health_alert
from src.db.supabase_client import (
    get_previous_run,
    log_scrape_run,
    upsert_analysis,
    upsert_raw_listing,
)

CONFIG_DIR = PROJECT_ROOT / "config"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(PROJECT_ROOT / "flipcar.log"),
    ],
)
logger = logging.getLogger(__name__)


def load_all_params() -> dict[str, Any]:
    """Load all parameters from config."""
    with open(CONFIG_DIR / "params.yaml") as f:
        return yaml.safe_load(f)


def analyze_listing(
    listing: dict[str, Any],
    all_listings: list[dict[str, Any]],
    params: dict[str, Any],
) -> dict[str, Any] | None:
    """Run the full analysis pipeline on a single listing.

    Args:
        listing: Normalized listing dict.
        all_listings: All listings for comp selection.
        params: Full params dict.

    Returns:
        Complete audit record, or None on failure.
    """
    listing_id = listing.get("listing_id", "unknown")

    try:
        # a. SVV enrichment
        enrich_listing(listing)

        # b. Find comps
        comp_result = find_comps(listing, all_listings, params)

        # c. Calculate FMV
        fmv_raw = calculate_fmv(comp_result, params)

        # d. Detect and apply adjustments
        adjustments = detect_adjustments(listing)
        fmv_adjusted = apply_adjustments(fmv_raw, adjustments)

        # e. Estimate repairs
        rep = estimate_repairs(listing, params=params)

        # f. Days to sell
        days = estimate_days(listing, fmv_adjusted["adjusted_p50"], params)

        # g. Profit calculation
        profit_result = calculate_profit(listing, fmv_adjusted, rep, days, params)

        # h. MPP
        mpp_data = calculate_mpp(fmv_adjusted, rep, days, params)

        # i. Classify
        classification = classify_deal(profit_result, comp_result, listing, params)

        # Build audit record
        record = build_audit_record(
            listing, comp_result, fmv_raw, fmv_adjusted,
            rep, days, profit_result, mpp_data, classification,
        )

        logger.info(
            "Analyzed %s: %s %s %s - %s (base: %s kr)",
            listing_id,
            listing.get("make"),
            listing.get("model"),
            listing.get("variant"),
            classification["classification"],
            profit_result.get("scenarios", {}).get("80pct_loan", {}).get("profit_base"),
        )

        return record

    except Exception as e:
        logger.error("Failed to analyze listing %s: %s", listing_id, e, exc_info=True)
        return None


def check_health(
    results: dict[str, list[dict[str, Any]]],
    params: dict[str, Any],
) -> str:
    """Check scrape health and send alerts if needed.

    Args:
        results: Per-model scrape results.
        params: Full params dict.

    Returns:
        Health status string: 'OK', 'WARNING', or 'ALARM'.
    """
    health_params = params["health"]
    total = sum(len(v) for v in results.values())
    per_model = {k: len(v) for k, v in results.items()}

    previous = get_previous_run()
    status = "OK"

    if previous:
        prev_total = previous.get("total_listings", 0)
        if prev_total > 0 and total < prev_total * health_params["min_listings_pct_of_previous"]:
            status = "ALARM"
            send_health_alert(
                f"Listings dropped: {total} vs previous {prev_total} "
                f"({total/prev_total:.0%})"
            )

        prev_per_model = previous.get("listings_per_model", {})
        for model_key, count in per_model.items():
            prev_count = prev_per_model.get(model_key, 0)
            if prev_count > 0:
                change = abs(count - prev_count) / prev_count
                if change > health_params["max_price_change_pct"]:
                    if status != "ALARM":
                        status = "WARNING"
                    send_health_alert(
                        f"Model {model_key}: count changed {prev_count} -> {count} "
                        f"({change:.0%})"
                    )

    logger.info("Health check: %s (total=%d)", status, total)
    return status


def run() -> None:
    """Execute the full pipeline: scrape -> analyze -> output."""
    load_dotenv()
    params = load_all_params()

    logger.info("=== FlipCar Pipeline Start ===")
    start_time = datetime.now(timezone.utc)

    # 1. Scrape
    logger.info("Step 1: Scraping FINN.no...")
    config = load_config()
    results = scrape_all_models(config)
    all_listings = flatten_results(results)
    logger.info("Scraped %d total listings", len(all_listings))

    # 2. Store raw listings
    logger.info("Step 2: Storing raw listings...")
    for listing in all_listings:
        upsert_raw_listing(listing)

    # 3. Analyze each listing
    logger.info("Step 3: Analyzing listings...")
    audit_records: list[dict[str, Any]] = []
    errors: list[str] = []

    for listing in all_listings:
        record = analyze_listing(listing, all_listings, params)
        if record:
            audit_records.append(record)
            # Store analysis
            upsert_analysis(record)
        else:
            errors.append(f"Failed: {listing.get('listing_id')}")

    logger.info("Analyzed %d / %d listings", len(audit_records), len(all_listings))

    # 4. Send Telegram alerts
    logger.info("Step 4: Sending alerts...")
    alerts_sent = 0
    for record in audit_records:
        # Flatten for telegram format
        alert_data = {
            **record,
            "fmv": record.get("fmv", {}),
            "comps": record.get("comps", {}),
            "mpp_data": {"mpp": record.get("mpp", 0)},
        }
        if send_deal_alert(alert_data):
            if "KONTAKT" in record.get("classification", ""):
                alerts_sent += 1

    logger.info("Sent %d Telegram alerts", alerts_sent)

    # 5. Write output files
    logger.info("Step 5: Writing output files...")
    output_dir = PROJECT_ROOT
    write_jsonl(audit_records, str(output_dir / "deals.jsonl"))
    write_csv(audit_records, str(output_dir / "deals.csv"))

    # 6. Health check
    logger.info("Step 6: Health check...")
    health_status = check_health(results, params)
    per_model = {k: len(v) for k, v in results.items()}
    log_scrape_run(len(all_listings), per_model, errors, health_status)

    elapsed = (datetime.now(timezone.utc) - start_time).total_seconds()
    logger.info(
        "=== FlipCar Pipeline Complete === (%.1fs, %d listings, %d analyzed, %d alerts)",
        elapsed, len(all_listings), len(audit_records), alerts_sent,
    )


if __name__ == "__main__":
    run()
