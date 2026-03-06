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
    """Send a message via Telegram Bot API.

    Args:
        text: Message text (supports Telegram MarkdownV2 or plain text).
        token: Bot token override (uses env var if empty).
        chat_id: Chat ID override (uses env var if empty).

    Returns:
        True if message was sent successfully.
    """
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


def format_deal_alert(analysis: dict[str, Any]) -> str:
    """Format a deal analysis into a Telegram alert message.

    Args:
        analysis: Full analysis dict from the pipeline.

    Returns:
        Formatted message string.
    """
    clf = analysis.get("classification", "")
    make = analysis.get("make", "")
    model = analysis.get("model", "")
    variant = analysis.get("variant", "")
    year = analysis.get("year", "")
    km = analysis.get("km", 0)
    location = analysis.get("location", "")
    price = analysis.get("price_nok", 0)
    url = analysis.get("listing_url", "")

    fmv = analysis.get("fmv", {})
    comps = analysis.get("comps", {})
    mpp_data = analysis.get("mpp_data", {})
    days = analysis.get("days", {})
    flags = analysis.get("flags", [])

    scenario_80 = analysis.get("scenarios", {}).get("80pct_loan", {})

    mpp_val = mpp_data.get("mpp", 0)
    required_discount = analysis.get("required_discount", "")

    lines = [
        f"<b>{clf}</b>",
        "",
        f"{make} {model} {variant} {year} | {km:,} km",
        f"Lokasjon: {location}",
        f"Pris: {price:,} kr",
        "",
        f"FMV adjusted: {fmv.get('adjusted_p50', 0):,} kr ({comps.get('n_comps', 0)} comps, Tier {comps.get('tier', '?')})",
        f"MPP: {mpp_val:,} kr (trenger {required_discount} rabatt)",
        "",
        "Profitt (80% laan):",
        f"  Bull: {scenario_80.get('profit_bull', 0):+,} kr",
        f"  Base: {scenario_80.get('profit_base', 0):+,} kr",
        f"  Bear: {scenario_80.get('profit_bear', 0):+,} kr",
        "",
        f"ROE ann. base: {scenario_80.get('roe_base', 'N/A')}",
        "",
        f"Days to sell: {days.get('p50', 0)} (bear: {days.get('p90', 0)})",
        f"Laan-anbefaling: {analysis.get('loan_recommendation', '')}",
    ]

    if flags:
        lines.append("")
        for flag in flags:
            lines.append(flag)

    lines.append("")
    lines.append(f'<a href="{url}">Se annonse</a>')

    return "\n".join(lines)


def send_deal_alert(analysis: dict[str, Any]) -> bool:
    """Send a deal alert if classification warrants it.

    Args:
        analysis: Full analysis dict from the pipeline.

    Returns:
        True if alert was sent (or not needed).
    """
    clf = analysis.get("classification", "")
    if "KONTAKT" in clf:
        msg = format_deal_alert(analysis)
        return send_message(msg)
    return True


def send_health_alert(message: str) -> bool:
    """Send a health monitoring alert.

    Args:
        message: Alert message text.

    Returns:
        True if sent successfully.
    """
    return send_message(f"<b>HEALTH ALERT</b>\n\n{message}")
