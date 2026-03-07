"""Reference registration number registry for Pristips lookups."""

import logging
from typing import Any

from src.db.supabase_client import upsert_regnr_registry

logger = logging.getLogger(__name__)


def _make_key(make: str, model: str, variant: str, year: int) -> str:
    return f"{make}_{model}_{variant}_{year}".lower().replace(" ", "_").replace("-", "_")


def build_regnr_registry(all_listings: list[dict[str, Any]]) -> dict[str, str]:
    """Build a registry of reference regnr per make/model/variant/year.

    From all scraped listings, pick one regnr per unique combination.
    Also builds fallback keys with variant='any'.
    """
    registry: dict[str, str] = {}

    for listing in all_listings:
        regnr = listing.get("registration_number")
        if not regnr:
            continue

        key = _make_key(
            listing.get("make", ""),
            listing.get("model", ""),
            listing.get("variant", "unknown"),
            listing.get("year", 0),
        )
        if key not in registry:
            registry[key] = regnr

    # Fallback: any-variant keys
    for listing in all_listings:
        regnr = listing.get("registration_number")
        if not regnr:
            continue

        fallback_key = _make_key(
            listing.get("make", ""),
            listing.get("model", ""),
            "any",
            listing.get("year", 0),
        )
        if fallback_key not in registry:
            registry[fallback_key] = regnr

    return registry


def get_reference_regnr(
    registry: dict[str, str],
    make: str,
    model: str,
    variant: str,
    year: int,
) -> str | None:
    """Find best reference regnr for a listing without its own regnr."""
    # 1. Exact: same variant + year
    key = _make_key(make, model, variant, year)
    if key in registry:
        return registry[key]

    # 2. Any variant + same year
    key = _make_key(make, model, "any", year)
    if key in registry:
        return registry[key]

    # 3. Any variant + year +/-1
    for dy in [-1, 1]:
        key = _make_key(make, model, "any", year + dy)
        if key in registry:
            return registry[key]

    return None


def save_registry(registry: dict[str, str], all_listings: list[dict[str, Any]]) -> None:
    """Persist registry to Supabase."""
    saved = 0
    seen_keys: set[str] = set()

    for listing in all_listings:
        regnr = listing.get("registration_number")
        if not regnr:
            continue

        make = listing.get("make", "")
        model = listing.get("model", "")
        variant = listing.get("variant", "unknown")
        year = listing.get("year", 0)
        key = _make_key(make, model, variant, year)

        if key in seen_keys:
            continue
        seen_keys.add(key)

        if upsert_regnr_registry(make, model, variant, year, regnr):
            saved += 1

    logger.info("Saved %d regnr entries to registry", saved)
