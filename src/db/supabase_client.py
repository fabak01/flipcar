"""Supabase database operations for storing scrape results and analyses."""

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

# SQL for table creation (run manually in Supabase dashboard)
CREATE_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS raw_listings (
  id SERIAL PRIMARY KEY,
  listing_id TEXT UNIQUE,
  scraped_at TIMESTAMPTZ DEFAULT NOW(),
  raw_data JSONB
);

CREATE TABLE IF NOT EXISTS analyzed_listings (
  id SERIAL PRIMARY KEY,
  listing_id TEXT UNIQUE REFERENCES raw_listings(listing_id),
  analyzed_at TIMESTAMPTZ DEFAULT NOW(),
  analysis JSONB,
  classification TEXT,
  profit_base INTEGER,
  profit_bear INTEGER,
  mpp INTEGER
);

CREATE TABLE IF NOT EXISTS scrape_runs (
  id SERIAL PRIMARY KEY,
  run_at TIMESTAMPTZ DEFAULT NOW(),
  total_listings INTEGER,
  listings_per_model JSONB,
  errors JSONB,
  health_status TEXT
);
"""


def _get_client() -> Optional[Any]:
    """Initialize Supabase client from environment variables."""
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_SERVICE_KEY") or os.getenv("SUPABASE_ANON_KEY")

    if not url or not key:
        logger.warning("Supabase credentials not configured, database operations will be skipped")
        return None

    try:
        from supabase import create_client
        return create_client(url, key)
    except ImportError:
        logger.error("supabase package not installed, run: pip install supabase")
        return None
    except Exception as e:
        logger.error("Failed to create Supabase client: %s", e)
        return None


def upsert_raw_listing(listing: dict[str, Any]) -> bool:
    """Insert or update a raw listing in the database.

    Args:
        listing: Raw listing dict from scraper.

    Returns:
        True if successful.
    """
    client = _get_client()
    if not client:
        return False

    listing_id = listing.get("listing_id")
    if not listing_id:
        return False

    try:
        client.table("raw_listings").upsert({
            "listing_id": listing_id,
            "scraped_at": datetime.now(timezone.utc).isoformat(),
            "raw_data": json.loads(json.dumps(listing, default=str)),
        }, on_conflict="listing_id").execute()
        return True
    except Exception as e:
        logger.error("Failed to upsert raw listing %s: %s", listing_id, e)
        return False


def upsert_analysis(analysis: dict[str, Any]) -> bool:
    """Insert or update an analyzed listing.

    Args:
        analysis: Full audit trail dict.

    Returns:
        True if successful.
    """
    client = _get_client()
    if not client:
        return False

    listing_id = analysis.get("listing_id")
    if not listing_id:
        return False

    scenario_80 = analysis.get("scenarios", {}).get("80pct_loan", {})

    try:
        client.table("analyzed_listings").upsert({
            "listing_id": listing_id,
            "analyzed_at": datetime.now(timezone.utc).isoformat(),
            "analysis": json.loads(json.dumps(analysis, default=str)),
            "classification": analysis.get("classification"),
            "profit_base": scenario_80.get("profit_base"),
            "profit_bear": scenario_80.get("profit_bear"),
            "mpp": analysis.get("mpp"),
        }, on_conflict="listing_id").execute()
        return True
    except Exception as e:
        logger.error("Failed to upsert analysis %s: %s", listing_id, e)
        return False


def log_scrape_run(
    total: int,
    per_model: dict[str, int],
    errors: list[str],
    health_status: str,
) -> bool:
    """Log a scrape run's metadata.

    Args:
        total: Total listings scraped.
        per_model: Count per model key.
        errors: List of error messages.
        health_status: 'OK', 'WARNING', or 'ALARM'.

    Returns:
        True if successful.
    """
    client = _get_client()
    if not client:
        return False

    try:
        client.table("scrape_runs").insert({
            "run_at": datetime.now(timezone.utc).isoformat(),
            "total_listings": total,
            "listings_per_model": per_model,
            "errors": errors,
            "health_status": health_status,
        }).execute()
        return True
    except Exception as e:
        logger.error("Failed to log scrape run: %s", e)
        return False


def get_previous_run() -> Optional[dict[str, Any]]:
    """Get the most recent scrape run for health comparison.

    Returns:
        Previous run data or None.
    """
    client = _get_client()
    if not client:
        return None

    try:
        resp = (
            client.table("scrape_runs")
            .select("*")
            .order("run_at", desc=True)
            .limit(1)
            .execute()
        )
        if resp.data:
            return resp.data[0]
    except Exception as e:
        logger.error("Failed to get previous run: %s", e)
    return None
