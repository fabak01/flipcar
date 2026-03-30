"""Telegram bot for deal notifications and health alerts.

Output: NO emojis. Clean, concise, action-oriented.
Sends for execution_gate SEND and SEND_NO_AI.
"""

import logging
import os
from typing import Any

import requests

logger = logging.getLogger(__name__)

# Human-readable labels for catalog adjustment types
_ADJ_LABELS: dict[str, str] = {
    "eu_kontroll_godkjent_fersk": "EU-kontroll fersk",
    "eu_kontroll_godkjent": "EU-kontroll gyldig",
    "eu_kontroll_forfalt": "EU-kontroll forfalt",
    "eu_kontroll_snart_forfalt": "EU-kontroll snart forfalt",
    "eu_kontroll_nær_forfall": "EU-kontroll nær forfall",
    "eu_kontroll_ikke_nevnt": "EU-kontroll ikke nevnt",
    "dekk_nye_vinter_og_sommer": "Nye dekk (vinter+sommer)",
    "dekk_nye_vinterdekk": "Nye vinterdekk",
    "dekk_slitte_dekk": "Slitte dekk",
    "dekk_mangler_sett": "Mangler dekksett",
    "service_full_historikk": "Full servicehistorikk",
    "service_delvis": "Delvis servicehistorikk",
    "service_ingen": "Ingen servicehistorikk",
    "service_forfalt": "Service forfalt",
    "batteri_soh_over_90": "SOH >90%",
    "batteri_soh_85_90": "SOH 85-90%",
    "batteri_soh_80_85": "SOH 80-85%",
    "batteri_soh_under_80": "SOH <80%",
    "batteri_soh_ikke_oppgitt": "SOH ikke oppgitt",
    "batteri_soh_garanti_gjenstar": "Batterigaranti gjenstår",
    "skade_smaskader": "Småskader",
    "skade_bulk_riper": "Bulk/riper",
    "skade_rust": "Rust",
    "skade_selges_som_den_er": "Selges som den er",
}


def _adj_label(adj_type: str) -> str:
    """Return a human-readable label for a catalog adjustment type."""
    if adj_type in _ADJ_LABELS:
        return _ADJ_LABELS[adj_type]
    return adj_type.replace("_", " ").title()


def _get_credentials() -> tuple[str, str]:
    """Get Telegram bot token and chat ID from environment."""
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    return token, chat_id


def send_message(text: str, token: str = "", chat_id: str = "") -> bool:
    """Send a message via Telegram Bot API."""
    if not token or not chat_id:
        token, chat_id = _get_credentials()
    if not token or not chat_id:
        logger.warning("Telegram credentials not configured, skipping notification")
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }

    try:
        resp = requests.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        logger.info("Telegram message sent successfully")
        return True
    except requests.RequestException as e:
        logger.error("Failed to send Telegram message: %s", e)
        return False


def fmt(n: Any) -> str:
    """Format numbers with Norwegian thousand separators (space)."""
    if n is None:
        return "N/A"
    return f"{int(round(n)):,}".replace(",", " ")


def format_deal_message(deal: dict[str, Any]) -> str:
    """Format a deal into a clean, structured Telegram message."""
    c = deal.get("classification", {})
    l = deal.get("listing", {})
    m = deal.get("market", {})
    adj_detail = deal.get("adjustments_detail", {})

    label = c.get("label", "PASS")
    reason = c.get("reason", "")

    make = l.get("make", "")
    model = l.get("model", "")
    year = l.get("year", "")
    km = l.get("km", 0)
    variant = (l.get("variant") or "").strip()
    location = l.get("location_city") or l.get("location") or "Ukjent"
    listing_url = l.get("listing_url", "")

    seller_type = l.get("seller_type", "")
    seller_label = "Forhandler" if seller_type == "forhandler" else "Privat"

    lines: list[str] = []

    # ── Header ──
    lines.append(f"{label} — {reason}")
    lines.append("")

    # ── Car info ──
    car_parts = [p for p in [make, model, variant if variant.lower() != "unknown" else "", str(year)] if p]
    car_line = " ".join(car_parts)
    km_str = f"{fmt(km)} km" if km else "N/A km"
    lines.append(f"{car_line} | {km_str}")
    lines.append(f"Selger: {seller_label}")
    lines.append(f"Lokasjon: {location}")
    lines.append(f"Pris: {fmt(deal.get('asking_price'))} kr")
    lines.append("")

    # ── MARKED ──
    anchor = m.get("anchor")
    low = m.get("low")
    high = m.get("high")
    dts = m.get("days_to_sell")
    active = m.get("active_similar")
    sold = m.get("sold_90d")

    lines.append("── MARKED ──")
    lines.append(f"Pristips: {fmt(anchor)} kr")
    lines.append(f"Intervall: {fmt(low)} – {fmt(anchor)} – {fmt(high)} kr")
    lines.append(f"Salgstid: {fmt(dts) if dts is not None else 'N/A'} dager")
    lines.append(f"Aktive lignende: {active if active is not None else 'N/A'}")
    lines.append(f"Solgt siste 90d: {sold if sold is not None else 'N/A'}")
    lines.append("")

    # ── UNDERWRITING ──
    lines.append("── UNDERWRITING ──")
    adj_lines: list[str] = []

    for a in adj_detail.get("catalog", []):
        amount = a.get("amount", 0)
        if amount == 0:
            continue
        label_str = _adj_label(a.get("type", ""))
        sign = "+" if amount > 0 else "-"
        adj_lines.append(f"{sign} {label_str}: {fmt(abs(amount))} kr")

    for s in adj_detail.get("spec_matches", []):
        amount = s.get("amount_nok", 0)
        if amount == 0:
            continue
        spec_label = (s.get("matched_alias") or s.get("spec", "")).capitalize()
        sign = "+" if amount > 0 else "-"
        adj_lines.append(f"{sign} {spec_label}: {fmt(abs(amount))} kr")

    if adj_lines:
        lines.extend(adj_lines)
    else:
        lines.append("Ingen justeringer")

    rep = deal.get("repair_buffer", 0)
    lines.append(f"Rep-buffer: -{fmt(rep)} kr")
    lines.append("")

    # ── RESULTAT ──
    spread_ask = deal.get("spread_ask_pct", 0)
    realistic_bid = deal.get("realistic_bid_price", 0)
    spread_bid = deal.get("spread_bid_pct", 0)
    confidence = deal.get("confidence", "MEDIUM")

    lines.append("── RESULTAT ──")
    lines.append(f"Justert FMV: {fmt(deal.get('adjusted_market_value'))} kr")
    lines.append(f"Spread at ask: {spread_ask:.1%}")
    lines.append(f"Spread at bid ({fmt(realistic_bid)}): {spread_bid:.1%}")
    lines.append(f"Confidence: {confidence}")
    lines.append("")

    # ── Flags ──
    flags: list[str] = []
    soh_status = deal.get("soh_status", "")
    eu_status = deal.get("eu_status", "")
    if soh_status in ("not_mentioned", "unknown"):
        flags.append("SOH IKKE OPPGITT")
    if deal.get("high_text_dependency"):
        flags.append("HØY TEKSTAVHENGIGHET")
    if deal.get("adj_positive_capped"):
        flags.append(
            f"JUSTERINGER BEGRENSET ({fmt(deal.get('adj_positive_raw'))} -> {fmt(deal.get('adj_positive'))} kr)"
        )
    if eu_status == "expired":
        flags.append("EU-KONTROLL FORFALT")
    elif eu_status == "expiring_soon":
        flags.append("EU-KONTROLL SNART FORFALT")

    for flag in flags:
        lines.append(flag)
    if flags:
        lines.append("")

    # ── SJEKK FØR KJØP ──
    lines.append("── SJEKK FØR KJØP ──")
    missing = deal.get("ai_missing_info", [])
    if missing and isinstance(missing, list):
        for item in missing[:5]:
            text = item.get("question", str(item)) if isinstance(item, dict) else str(item)
            lines.append(f"• {text}")
    else:
        # Fall back to negative then positive signals
        neg_signals = deal.get("ai_negative_signals", [])
        pos_signals = deal.get("ai_positive_signals", [])
        candidates = neg_signals[:3] or pos_signals[:3]
        if candidates:
            for sig in candidates:
                text = sig.get("name", str(sig)) if isinstance(sig, dict) else str(sig)
                lines.append(f"• {text}")
        else:
            lines.append("• Ingen spesifikke handlingspunkter")

    lines.append("")

    # ── URL (raw, last line — triggers Telegram link preview) ──
    if listing_url:
        lines.append(listing_url)

    return "\n".join(lines)


def send_deal_alert_new(deal: dict[str, Any]) -> bool:
    """Send a deal alert if execution_gate allows it."""
    # Hard block: rep listings use a borrowed regnr — valuation is less reliable
    if deal.get("is_rep_listing"):
        return True

    c = deal.get("classification", {})
    gate = c.get("execution_gate", "BLOCKED")

    if gate in ("SEND", "SEND_NO_AI"):
        msg = format_deal_message(deal)
        return send_message(msg)
    return True  # Not an error, just not sent


# Legacy interface
def format_deal_alert(analysis: dict[str, Any]) -> str:
    """Legacy format — redirect to new."""
    return format_deal_message(analysis)


def send_deal_alert(analysis: dict[str, Any]) -> bool:
    """Legacy send — redirect to new."""
    return send_deal_alert_new(analysis)


def send_health_alert(message: str) -> bool:
    """Send a health monitoring alert."""
    return send_message(f"[HEALTH ALERT]\n\n{message}")
