"""Main orchestrator with two-level architecture (screening + deep underwriting)."""

import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.db.supabase_client import (
    get_cached_text_analysis,
    get_previous_run,
    log_scrape_run,
    upsert_analysis,
    upsert_raw_listing,
    upsert_text_analysis_cache,
)
from src.engine.battery_soh import calculate_soh_sensitivity
from src.engine.classifier import classify_deal
from src.engine.comps import find_comps
from src.engine.days_to_sell import estimate_days_to_sell
from src.engine.pristips import get_pristips_cached
from src.engine.profit import calculate_profit
from src.engine.rep_estimator import estimate_repairs
from src.engine.screener import screen_listing
from src.engine.text_analyzer import analyze_listing_text
from src.engine.underwriting import calculate_underwritten_exit
from src.output.formatter import build_audit_record, write_csv, write_jsonl
from src.output.telegram_bot import send_deal_alert, send_health_alert
from src.scraper.finn_scraper import flatten_results, load_config, scrape_all_models

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


def _extract_reported_soh(listing: dict[str, Any], ai_analysis: dict[str, Any] | None = None) -> float | None:
    if ai_analysis:
        val = ai_analysis.get("condition_summary", {}).get("batteri_soh")
        if isinstance(val, (int, float)):
            return float(val)
    text = f"{listing.get('title', '')} {listing.get('listing_text', '')}".lower()
    m = re.search(r"(?:soh|battery\s*health|batterikapasitet)\s*[:=]?\s*(\d{2})(?:[.,](\d))?", text)
    if not m:
        return None
    value = float(m.group(1))
    if m.group(2):
        value += float(f"0.{m.group(2)}")
    return value if 40 <= value <= 100 else None


def _fuel_is_ev_or_phev(listing: dict[str, Any], model_cfg: dict[str, Any] | None = None) -> bool:
    fuel = str(listing.get("fuel_type", "")).lower()
    if any(k in fuel for k in ["el", "elektr", "electric", "plugin", "plug-in", "phev"]):
        return True
    if model_cfg and model_cfg.get("fuel_type") in {"electric", "plugin_hybrid"}:
        return True
    return False


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


def run_daily() -> None:
    load_dotenv()
    params = load_all_params()

    logger.info("=== FlipCar Daily Run Start ===")
    config = load_config()
    model_map = {f"{m['make']}_{m['model']}".lower().replace(' ', '_').replace('-', '').replace('.', ''): m for m in config["models"]}

    results = scrape_all_models(config)
    all_listings = flatten_results(results)

    for listing in all_listings:
        upsert_raw_listing(listing)

    shortlist: list[dict[str, Any]] = []
    for listing in all_listings:
        screen = screen_listing(listing, all_listings, params)
        listing["screening"] = screen
        if screen["passes_screening"]:
            shortlist.append(listing)

    logger.info("Screened %d listings -> %d shortlisted", len(all_listings), len(shortlist))

    audit_records: list[dict[str, Any]] = []
    errors: list[str] = []

    for listing in shortlist:
        try:
            regnr = listing.get("registration_number")
            km = listing.get("km") or 0
            pristips = get_pristips_cached(regnr, int(km)) if regnr and km else None

            cached_ai = get_cached_text_analysis(listing.get("listing_id", ""))
            if cached_ai:
                ai = cached_ai
            else:
                ai = analyze_listing_text(listing.get("listing_text", ""), listing.get("make", ""), listing.get("model", ""), int(listing.get("year") or 0))
                upsert_text_analysis_cache(listing.get("listing_id", ""), ai)

            comp_result = find_comps(listing, all_listings, params)
            underwriting = calculate_underwritten_exit(pristips, ai, comp_result, listing, params)

            fmv_adjusted = {
                "adjusted_p10": underwriting.get("underwritten_exit_bear", 0),
                "adjusted_p50": underwriting.get("underwritten_exit_base", 0),
                "adjusted_p90": underwriting.get("underwritten_exit_bull", 0),
                "adjustments": [],
            }
            fmv_raw = {"raw_p10": fmv_adjusted["adjusted_p10"], "raw_p50": fmv_adjusted["adjusted_p50"], "raw_p90": fmv_adjusted["adjusted_p90"]}

            rep = estimate_repairs(listing, params=params)
            days_new = estimate_days_to_sell(listing, pristips, params)
            days = {"p50": days_new["days_p50"], "p90": days_new["days_p90"], "bull": days_new["days_bull"], "source": days_new["source"]}
            profit = calculate_profit(listing, fmv_adjusted, rep, days, params, pristips=pristips)

            mpp = min(
                profit["assumed_entry_price"],
                max(0, int(profit["assumed_entry_price"] - max(0, profit["scenarios"]["80pct_loan"]["profit_base"]) + params["mpp"]["target_profit"])),
            )
            mpp_data = {"mpp": int(mpp), "mpp_base": int(mpp), "mpp_bear": int(mpp)}

            base_profit = profit["scenarios"]["80pct_loan"]["profit_base"]
            model_key = f"{listing.get('make','')}_{listing.get('model','')}".lower().replace(" ", "_").replace("-", "").replace(".", "")
            model_cfg = model_map.get(model_key)
            is_ev = _fuel_is_ev_or_phev(listing, model_cfg)
            soh_reported = _extract_reported_soh(listing, ai)
            soh = calculate_soh_sensitivity(base_profit, is_ev, soh_reported, listing.get("make", ""), listing.get("model", ""), int(listing.get("year") or 0))

            classification = classify_deal(profit, comp_result, listing, params, soh_analysis=soh)

            record = build_audit_record(listing, comp_result, fmv_raw, fmv_adjusted, rep, days, profit, mpp_data, classification, soh_analysis=soh)
            record["screening"] = listing.get("screening", {})
            record["pristips"] = pristips
            record["ai_analysis"] = ai
            record["underwriting"] = underwriting

            audit_records.append(record)
            upsert_analysis(record)

        except Exception as e:
            errors.append(f"{listing.get('listing_id')}: {e}")

    alerts_sent = 0
    for record in audit_records:
        alert_data = {**record, "fmv": record.get("fmv", {}), "comps": record.get("comps", {}), "mpp_data": {"mpp": record.get("mpp", 0)}, "soh_analysis": record.get("soh_analysis", {})}
        if send_deal_alert(alert_data) and "KONTAKT" in record.get("classification", ""):
            alerts_sent += 1

    write_jsonl(audit_records, str(PROJECT_ROOT / "deals.jsonl"))
    write_csv(audit_records, str(PROJECT_ROOT / "deals.csv"))

    health_status = check_health(results, params)
    per_model = {k: len(v) for k, v in results.items()}
    log_scrape_run(len(all_listings), per_model, errors, health_status)

    logger.info("=== FlipCar Daily Run Complete === (%d scraped, %d shortlisted, %d analyzed, %d alerts)", len(all_listings), len(shortlist), len(audit_records), alerts_sent)


def run() -> None:
    """Backward-compatible entrypoint."""
    run_daily()


if __name__ == "__main__":
    run_daily()
