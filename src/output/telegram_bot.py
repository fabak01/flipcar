"""Telegram bot for deal notifications and health alerts."""

import logging
import os
from typing import Any

import requests

logger = logging.getLogger(__name__)


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
    """Format numbers with thousand separators."""
    if n is None:
        return "N/A"
    return f"{int(round(n)):,}".replace(",", " ")


def format_deal_message(deal: dict[str, Any]) -> str:
    """Format a deal from underwrite_deal() into a Telegram alert message."""
    c = deal.get("classification", {})
    l = deal.get("listing", {})
    m = deal.get("market", {})
    s = deal.get("scenarios", {})

    # Header
    emoji = c.get("emoji", "")
    label = c.get("label", "")
    msg = f"<b>{emoji} {label}</b>\n\n"
    msg += f"<b>{l.get('make', '')} {l.get('model', '')} {l.get('variant', '')} {l.get('year', '')}</b> | {fmt(l.get('km'))} km\n"
    msg += f"{l.get('location_city', 'Ukjent')}\n"
    msg += f"Annonsepris: <b>{fmt(l.get('price_nok'))} kr</b>\n\n"

    # Market
    msg += "-- MARKED --\n"
    if m.get("anchor"):
        msg += f"Comps FMV: <b>{fmt(m['anchor'])} kr</b>\n"
        if m.get("low") and m.get("high"):
            msg += f"Intervall: {fmt(m['low'])} - {fmt(m['high'])} kr\n"
        regnr_note = " (ref.regnr)" if l.get("regnr_source") == "reference" else ""
        msg += f"Kilde: {m.get('source', 'comps')}{regnr_note}\n"
    else:
        msg += "Markedsdata: Utilgjengelig\n"

    if m.get("days_to_sell"):
        msg += f"Salgstid: ca. {m['days_to_sell']} dager\n"
    if m.get("active_similar"):
        msg += f"Aktive lignende: {m['active_similar']}"
        if m.get("sold_90d"):
            msg += f" | Solgt 90d: {m['sold_90d']}"
        msg += "\n"

    # Underwriting
    msg += "\n-- UNDERWRITING --\n"
    ai = deal.get("ai_analysis") or {}
    n_pos = len(ai.get("positives", []))
    n_iss = len(ai.get("issues", []))

    if n_pos > 0:
        pos_names = ", ".join(p.get("name", "") for p in ai["positives"][:3])
        msg += f"Positive ({n_pos}): {pos_names}\n"
    if n_iss > 0:
        iss_names = ", ".join(i.get("name", "") for i in ai["issues"][:3])
        msg += f"Issues ({n_iss}): {iss_names}\n"

    exit_data = deal.get("exit", {})
    msg += f"Exit base: {fmt(exit_data.get('base'))} kr\n"
    msg += f"Exit bear: {fmt(exit_data.get('bear'))} kr\n"

    # Profit (all scenarios)
    msg += "\n-- PROFITT --\n"
    msg += "<pre>"
    msg += f"{'':12} {'Cash':>10} {'60% laan':>10} {'80% laan':>10}\n"

    for row_label, key in [("Bull:", "profit_bull"), ("Base:", "profit_base"), ("Bear:", "profit_bear")]:
        cash_val = fmt(s.get("cash", {}).get(key))
        s60_val = fmt(s.get("60pct", {}).get(key))
        s80_val = fmt(s.get("80pct", {}).get(key))
        msg += f"{row_label:12} {cash_val:>10} {s60_val:>10} {s80_val:>10}\n"

    cash_roe = s.get("cash", {}).get("roe_base_annual", "N/A")
    s60_roe = s.get("60pct", {}).get("roe_base_annual", "N/A")
    s80_roe = s.get("80pct", {}).get("roe_base_annual", "N/A")
    msg += f"{'ROE ann:':12} {str(cash_roe) + '%':>10} {str(s60_roe) + '%':>10} {str(s80_roe) + '%':>10}\n"
    msg += "</pre>\n"

    # MPP
    msg += f"\nMPP: <b>{fmt(deal.get('mpp'))} kr</b>"
    rd = deal.get("required_discount", 0)
    if rd and rd > 0:
        msg += f" (trenger {rd:.1%} rabatt)"
    msg += "\n"

    # Entry
    e = deal.get("entry", {})
    msg += f"Antatt entry: {fmt(e.get('assumed_entry_price'))} kr ({e.get('total_discount', 0):.0%} rabatt)\n"
    msg += f"Laan-anbefaling: {c.get('loan_rec', 'N/A')}\n"

    # SOH (only EVs with missing SOH)
    soh = deal.get("soh") or {}
    if soh.get("applicable") and soh.get("soh_missing"):
        msg += f"\nSOH IKKE OPPGITT\n"
        if soh.get("min_profitable_soh"):
            msg += f"Loennsom hvis SOH >= {soh['min_profitable_soh']}%\n"
        if soh.get("expected_soh"):
            msg += f"Forventet for denne aargangen: ~{soh['expected_soh']}%\n"
        msg += f"Spoer selger om SOH/batteritest\n"

    # Diligence
    diligence = ai.get("diligence_items", [])
    if diligence:
        msg += "\n<b>SJEKK FOER KJOEP:</b>\n"
        for d in diligence[:5]:
            q = d.get("question", "")
            if q:
                msg += f"- {q}\n"

    # Link
    msg += f"\n<a href=\"{l.get('listing_url', '#')}\">Se annonse</a>"

    return msg


def send_deal_alert_new(deal: dict[str, Any]) -> bool:
    """Send a deal alert using the new underwriting format."""
    c = deal.get("classification", {})
    if c.get("send_telegram", False):
        msg = format_deal_message(deal)
        return send_message(msg)
    return True


# Legacy interface for backward compatibility
def format_deal_alert(analysis: dict[str, Any]) -> str:
    """Format a deal analysis into a Telegram alert (legacy format)."""
    clf = analysis.get("classification", "")
    make = analysis.get("make", "")
    model = analysis.get("model", "")
    variant = analysis.get("variant", "")
    year = analysis.get("year", "")
    km = analysis.get("km", 0)
    location = analysis.get("location", "")
    listing_price_nok = analysis.get("listing_price_nok", analysis.get("price_nok", 0))
    url = analysis.get("listing_url", "")

    fmv = analysis.get("fmv", {})
    comps = analysis.get("comps", {})
    mpp_data = analysis.get("mpp_data", {})
    days = analysis.get("days", {})

    scenario_80 = analysis.get("scenarios", {}).get("80pct_loan", {})

    lines = [
        f"<b>{clf}</b>",
        "",
        f"{make} {model} {variant} {year} | {km:,} km",
        f"Lokasjon: {location}",
        f"Pris: {listing_price_nok:,} kr",
        "",
        f"FMV: {fmv.get('adjusted_p50', 0):,} kr ({comps.get('n_comps', 0)} comps, Tier {comps.get('tier', '?')})",
        f"MPP: {mpp_data.get('mpp', 0):,} kr",
        "",
        "Profitt (80% laan):",
        f"  Bull: {scenario_80.get('profit_bull', 0):+,} kr",
        f"  Base: {scenario_80.get('profit_base', 0):+,} kr",
        f"  Bear: {scenario_80.get('profit_bear', 0):+,} kr",
        "",
        f"Days to sell: {days.get('p50', 0)} (bear: {days.get('p90', 0)})",
        f"Laan-anbefaling: {analysis.get('loan_recommendation', '')}",
        "",
        f'<a href="{url}">Se annonse</a>',
    ]

    return "\n".join(lines)


def send_deal_alert(analysis: dict[str, Any]) -> bool:
    """Send a deal alert if classification warrants it (legacy)."""
    clf = analysis.get("classification", "")
    if "KONTAKT" in clf:
        msg = format_deal_alert(analysis)
        return send_message(msg)
    return True


def send_health_alert(message: str) -> bool:
    """Send a health monitoring alert."""
    return send_message(f"<b>HEALTH ALERT</b>\n\n{message}")
