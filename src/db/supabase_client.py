"""Supabase database operations for storing scrape results and analyses."""

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

# SQL for table creation (run manually in Supabase dashboard)
CREATE_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS raw_listings (
  id SERIAL PRIMARY KEY,
  listing_id TEXT UNIQUE,
  scraped_at TIMESTAMPTZ DEFAULT NOW(),
  raw_data JSONB,
  first_seen_at TIMESTAMPTZ,
  last_seen_at TIMESTAMPTZ,
  n_price_cuts INTEGER DEFAULT 0,
  original_price INTEGER
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

CREATE TABLE IF NOT EXISTS pristips_cache (
  id SERIAL PRIMARY KEY,
  registration_number TEXT,
  km INTEGER,
  fetched_at TIMESTAMPTZ DEFAULT NOW(),
  data JSONB,
  UNIQUE(registration_number, km)
);

CREATE TABLE IF NOT EXISTS text_analysis_cache (
  id SERIAL PRIMARY KEY,
  listing_id TEXT UNIQUE,
  fetched_at TIMESTAMPTZ DEFAULT NOW(),
  data JSONB
);

CREATE TABLE IF NOT EXISTS price_history (
  id SERIAL PRIMARY KEY,
  listing_id TEXT,
  observed_at TIMESTAMPTZ DEFAULT NOW(),
  price_nok INTEGER
);

CREATE TABLE IF NOT EXISTS regnr_registry (
  id SERIAL PRIMARY KEY,
  make TEXT,
  model TEXT,
  variant TEXT,
  year INTEGER,
  registration_number TEXT,
  updated_at TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE(make, model, variant, year)
);

CREATE TABLE IF NOT EXISTS deal_status (
  listing_id TEXT PRIMARY KEY,
  status TEXT NOT NULL DEFAULT 'new',
  telegram_sent_at TIMESTAMPTZ,
  bid_amount INTEGER,
  bid_at TIMESTAMPTZ,
  notes TEXT,
  updated_at TIMESTAMPTZ DEFAULT NOW()
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


def clear_caches() -> bool:
    """Delete all rows from pristips_cache and text_analysis_cache."""
    client = _get_client()
    if not client:
        logger.warning("No Supabase client, skipping cache clear")
        return False
    try:
        client.table("pristips_cache").delete().neq("id", 0).execute()
        client.table("text_analysis_cache").delete().neq("id", 0).execute()
        logger.info("Cleared pristips_cache and text_analysis_cache")
        return True
    except Exception as e:
        logger.error("Failed to clear caches: %s", e)
        return False


_regnr_registry_warned = False


def upsert_regnr_registry(make: str, model: str, variant: str, year: int, registration_number: str) -> bool:
    """Upsert a reference registration number into the registry.

    Gracefully handles missing table (logs once, then silently skips).
    """
    global _regnr_registry_warned
    client = _get_client()
    if not client:
        return False
    try:
        client.table("regnr_registry").upsert({
            "make": make,
            "model": model,
            "variant": variant,
            "year": year,
            "registration_number": registration_number,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }, on_conflict="make,model,variant,year").execute()
        return True
    except Exception as e:
        err_str = str(e).lower()
        if "regnr_registry" in err_str or "relation" in err_str or "does not exist" in err_str or "42P01" in err_str:
            if not _regnr_registry_warned:
                logger.warning("regnr_registry table not found in Supabase. Skipping persistence. "
                               "Create it with the SQL in supabase_client.py if needed.")
                _regnr_registry_warned = True
            return False
        logger.error("Failed to upsert regnr_registry: %s", e)
        return False


def upsert_raw_listing(listing: dict[str, Any]) -> bool:
    """Insert or update a raw listing in the database."""
    client = _get_client()
    if not client:
        return False

    listing_id = listing.get("listing_id")
    if not listing_id:
        return False

    now = datetime.now(timezone.utc).isoformat()
    price = listing.get("price_nok")

    try:
        # Try full schema with price tracking columns
        try:
            existing = (
                client.table("raw_listings")
                .select("listing_id, original_price, n_price_cuts")
                .eq("listing_id", listing_id)
                .limit(1)
                .execute()
            )
            existing_row = existing.data[0] if existing.data else None

            n_cuts = int(existing_row.get("n_price_cuts", 0)) if existing_row else 0
            original_price = existing_row.get("original_price") if existing_row else price

            if existing_row and price is not None:
                prev_price = ((
                    client.table("price_history")
                    .select("price_nok")
                    .eq("listing_id", listing_id)
                    .order("observed_at", desc=True)
                    .limit(1)
                    .execute()
                ).data or [{}])[0].get("price_nok")
                if prev_price and price < prev_price:
                    n_cuts += 1

            row = {
                "listing_id": listing_id,
                "scraped_at": now,
                "last_seen_at": now,
                "n_price_cuts": n_cuts,
                "original_price": original_price,
                "raw_data": json.loads(json.dumps(listing, default=str)),
            }
            if not existing_row:
                row["first_seen_at"] = now

            client.table("raw_listings").upsert(row, on_conflict="listing_id").execute()

        except Exception:
            # Fallback: minimal schema (table may not have extended columns yet)
            client.table("raw_listings").upsert({
                "listing_id": listing_id,
                "scraped_at": now,
                "raw_data": json.loads(json.dumps(listing, default=str)),
            }, on_conflict="listing_id").execute()

        if price is not None:
            try:
                client.table("price_history").insert({
                    "listing_id": listing_id,
                    "price_nok": int(price),
                    "observed_at": now,
                }).execute()
            except Exception:
                pass  # price_history table may not exist yet
        return True
    except Exception as e:
        logger.error("Failed to upsert raw listing %s: %s", listing_id, e)
        return False


def upsert_analysis(analysis: dict[str, Any]) -> bool:
    """Insert or update an analyzed listing."""
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


def log_scrape_run(total: int, per_model: dict[str, int], errors: list[str], health_status: str) -> bool:
    """Log a scrape run's metadata."""
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
    """Get the most recent scrape run for health comparison."""
    client = _get_client()
    if not client:
        return None

    try:
        resp = client.table("scrape_runs").select("*").order("run_at", desc=True).limit(1).execute()
        if resp.data:
            return resp.data[0]
    except Exception as e:
        logger.error("Failed to get previous run: %s", e)
    return None


def get_cached_pristips(registration_number: str, km: int, max_age_days: int = 7) -> Optional[dict[str, Any]]:
    """Read cached Pristips response if newer than max_age_days."""
    client = _get_client()
    if not client:
        return None
    try:
        resp = (
            client.table("pristips_cache")
            .select("fetched_at,data")
            .eq("registration_number", registration_number)
            .eq("km", int(km))
            .order("fetched_at", desc=True)
            .limit(1)
            .execute()
        )
        if not resp.data:
            return None
        row = resp.data[0]
        ts = datetime.fromisoformat(str(row["fetched_at"]).replace("Z", "+00:00"))
        if datetime.now(timezone.utc) - ts > timedelta(days=max_age_days):
            return None
        return row.get("data")
    except Exception as e:
        logger.error("Failed to read pristips cache: %s", e)
        return None


def upsert_pristips_cache(registration_number: str, km: int, data: dict[str, Any]) -> bool:
    """Store Pristips response in cache."""
    client = _get_client()
    if not client:
        return False
    try:
        client.table("pristips_cache").upsert({
            "registration_number": registration_number,
            "km": int(km),
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "data": json.loads(json.dumps(data, default=str)),
        }, on_conflict="registration_number,km").execute()
        return True
    except Exception as e:
        logger.error("Failed to write pristips cache: %s", e)
        return False


def get_cached_text_analysis(listing_id: str) -> Optional[dict[str, Any]]:
    """Read cached text analysis for listing."""
    client = _get_client()
    if not client:
        return None
    try:
        resp = client.table("text_analysis_cache").select("data").eq("listing_id", listing_id).limit(1).execute()
        if resp.data:
            return resp.data[0].get("data")
    except Exception as e:
        logger.error("Failed to read text analysis cache: %s", e)
    return None


def upsert_text_analysis_cache(listing_id: str, data: dict[str, Any]) -> bool:
    """Store text analysis cache."""
    client = _get_client()
    if not client:
        return False
    try:
        client.table("text_analysis_cache").upsert({
            "listing_id": listing_id,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "data": json.loads(json.dumps(data, default=str)),
        }, on_conflict="listing_id").execute()
        return True
    except Exception as e:
        logger.error("Failed to write text analysis cache: %s", e)
        return False


# ── Deal status tracking ─────────────────────────────────────────────────────

def get_deals_for_dashboard(limit: int = 300) -> list[dict[str, Any]]:
    """Fetch analyzed listings joined with deal status for the web dashboard.

    Returns a flat list of deal dicts, each with a 'status_data' key.
    """
    client = _get_client()
    if not client:
        return []

    try:
        resp = (
            client.table("analyzed_listings")
            .select("listing_id, analyzed_at, analysis")
            .order("analyzed_at", desc=True)
            .limit(limit)
            .execute()
        )
        rows = resp.data or []
    except Exception as e:
        logger.error("Failed to fetch deals for dashboard: %s", e)
        return []

    if not rows:
        return []

    listing_ids = [r["listing_id"] for r in rows]

    # Batch-fetch statuses
    statuses: dict[str, Any] = {}
    try:
        st_resp = client.table("deal_status").select("*").in_("listing_id", listing_ids).execute()
        statuses = {r["listing_id"]: r for r in (st_resp.data or [])}
    except Exception:
        pass  # deal_status table may not exist yet — degrade gracefully

    deals: list[dict[str, Any]] = []
    for row in rows:
        a: dict[str, Any] = row.get("analysis") or {}
        deal = dict(a)
        deal["listing_id"] = row["listing_id"]
        deal["analyzed_at"] = row.get("analyzed_at", "")
        deal["status_data"] = statuses.get(row["listing_id"], {"status": "new"})
        deals.append(deal)

    return deals


def get_deal_detail(listing_id: str) -> Optional[dict[str, Any]]:
    """Fetch a single deal with full analysis and status."""
    client = _get_client()
    if not client:
        return None

    try:
        resp = (
            client.table("analyzed_listings")
            .select("listing_id, analyzed_at, analysis")
            .eq("listing_id", listing_id)
            .limit(1)
            .execute()
        )
        if not resp.data:
            return None
        row = resp.data[0]
    except Exception as e:
        logger.error("Failed to fetch deal detail %s: %s", listing_id, e)
        return None

    a: dict[str, Any] = row.get("analysis") or {}
    deal = dict(a)
    deal["listing_id"] = listing_id
    deal["analyzed_at"] = row.get("analyzed_at", "")

    try:
        st_resp = client.table("deal_status").select("*").eq("listing_id", listing_id).limit(1).execute()
        deal["status_data"] = st_resp.data[0] if st_resp.data else {"status": "new"}
    except Exception:
        deal["status_data"] = {"status": "new"}

    return deal


def upsert_deal_status(listing_id: str, **fields: Any) -> bool:
    """Upsert deal status. Pass keyword args for fields to update."""
    client = _get_client()
    if not client:
        return False

    payload: dict[str, Any] = {
        "listing_id": listing_id,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    payload.update({k: v for k, v in fields.items() if v is not None and v != ""})

    try:
        client.table("deal_status").upsert(payload, on_conflict="listing_id").execute()
        return True
    except Exception as e:
        logger.error("Failed to upsert deal_status %s: %s", listing_id, e)
        return False


def mark_telegram_sent(listing_id: str) -> bool:
    """Record that a Telegram deal alert was sent. Sets status to 'contacted' if still 'new'."""
    client = _get_client()
    if not client:
        return False

    now = datetime.now(timezone.utc).isoformat()
    try:
        # Fetch current status to avoid overwriting a more advanced state
        existing = client.table("deal_status").select("status").eq("listing_id", listing_id).limit(1).execute()
        current_status = (existing.data[0].get("status") if existing.data else None) or "new"
        new_status = "contacted" if current_status == "new" else current_status

        client.table("deal_status").upsert({
            "listing_id": listing_id,
            "telegram_sent_at": now,
            "status": new_status,
            "updated_at": now,
        }, on_conflict="listing_id").execute()
        return True
    except Exception as e:
        logger.error("Failed to mark_telegram_sent %s: %s", listing_id, e)
        return False
