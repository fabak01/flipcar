"""Statens vegvesen (SVV) API lookup for vehicle data."""

import logging
import os
from datetime import datetime
from typing import Any, Optional

import requests

logger = logging.getLogger(__name__)


def lookup_vehicle(registration_number: str) -> Optional[dict[str, Any]]:
    """Look up vehicle data from SVV API by registration number.

    Args:
        registration_number: Norwegian vehicle registration number (e.g. 'AB12345').

    Returns:
        Dict with EU control dates and first registration, or None on failure.
    """
    api_key = os.getenv("SVV_API_KEY")
    if not api_key:
        logger.warning("SVV_API_KEY not set, skipping SVV lookup")
        return None

    regnr = registration_number.strip().upper().replace(" ", "")
    url = (
        f"https://akfell-datautlevering.atlas.vegvesen.no"
        f"/enkeltoppslag/kjoretoydata?kjennemerke={regnr}"
    )
    headers = {"SVV-Authorization": f"Apikey {api_key}"}

    try:
        resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        logger.error("SVV lookup failed for %s: %s", regnr, e)
        return None

    return _extract_svv_data(data)


def _extract_svv_data(data: dict[str, Any]) -> dict[str, Any]:
    """Extract relevant fields from SVV API response."""
    result: dict[str, Any] = {}

    # Navigate the nested SVV response structure
    godkjenning = data.get("kjoretoydataListe", [{}])[0] if isinstance(data.get("kjoretoydataListe"), list) else {}
    if not godkjenning and isinstance(data, dict):
        godkjenning = data

    periodisk = godkjenning.get("periodiskKjoretoyKontroll", godkjenning.get("godkjenning", {}))
    if isinstance(periodisk, dict):
        result["eu_kontroll_sist"] = periodisk.get(
            "sistGodkjent",
            periodisk.get("kontrollfrist", {}).get("sistGodkjent"),
        )
        result["eu_kontroll_frist"] = periodisk.get(
            "nesteFrist",
            periodisk.get("kontrollfrist", {}).get("nesteFrist"),
        )

    forste_reg = godkjenning.get("forstegangsregistrering", godkjenning.get("forstegangRegistrertDato"))
    if isinstance(forste_reg, dict):
        forste_reg = forste_reg.get("registrertForstegangNorgeDato")
    result["forstegangsregistrering"] = forste_reg

    return result


def enrich_listing(listing: dict[str, Any]) -> dict[str, Any]:
    """Enrich a listing with SVV data if registration number is available.

    Args:
        listing: A normalized listing dict.

    Returns:
        The listing dict, potentially enriched with SVV data.
    """
    reg_nr = listing.get("registration_number")
    if not reg_nr:
        return listing

    svv_data = lookup_vehicle(reg_nr)
    if svv_data:
        listing["svv_data"] = svv_data
        logger.info("Enriched listing %s with SVV data", listing.get("listing_id"))
    return listing
