"""Telegram bot for deal notifications and health alerts.

Output: NO emojis. Clean, concise, action-oriented.
Sends for execution_gate SEND and SEND_NO_AI.
"""

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
        "disable_web_page_preview": True,
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
    """Format a deal into a clean, no-emoji Telegram message."""
    c = deal.get("classification", {})
    l = deal.get("listing", {})
    m = deal.get("market", {})

    label = c.get("label", "PASS")
    make = l.get("make", "")
    model = l.get("model", "")
    year = l.get("year", "")
    km = l.get("km", 0)
    variant = l.get("variant", "")

    msg = f"[{label}] {make} {model}"
    if variant and variant != "unknown":
        msg += f" {variant}"
    msg += f" {year}"
    if km:
        msg += f" — {fmt(km)} km"
    msg += "\n\n"

    # Price + market
    msg += f"Ask: {fmt(deal.get('asking_price'))}\n"
    msg += f"Pristips: {fmt(m.get('anchor'))}\n"
    msg += f"Adj. market value: {fmt(deal.get('adjusted_market_value'))}\n"

    spread_ask = deal.get("spread_ask_pct", 0)
    msg += f"Spread at ask: {spread_ask:.1%}\n"

    realistic_bid = deal.get("realistic_bid_price", 0)
    spread_bid = deal.get("spread_bid_pct", 0)
    msg += f"Spread at realistic bid ({fmt(realistic_bid)}): {spread_bid:.1%}\n"

    # Top reasons
    pos_signals = deal.get("ai_positive_signals", [])
    neg_signals = deal.get("ai_negative_signals", [])

    if pos_signals or neg_signals:
        msg += "\nTop reasons:\n"
        for p in (pos_signals[:3] if isinstance(pos_signals, list) else []):
            name = p.get("name", p) if isinstance(p, dict) else str(p)
            msg += f"+ {name}\n"
        for n in (neg_signals[:3] if isinstance(neg_signals, list) else []):
            name = n.get("name", n) if isinstance(n, dict) else str(n)
            msg += f"- {name}\n"

    # AI summary
    ai_summary = deal.get("ai_summary_short", "")
    msg += "\nAI:\n"
    if ai_summary:
        msg += f"{ai_summary}\n"
    else:
        msg += "AI unavailable — rule-based only\n"

    # Action
    missing = deal.get("ai_missing_info", [])
    if missing:
        items = []
        for item in missing[:3]:
            if isinstance(item, dict):
                items.append(item.get("question", str(item)))
            else:
                items.append(str(item))
        if items:
            msg += f"\nAction:\n"
            msg += ". ".join(items) + "\n"

    # Link
    url = l.get("listing_url", "")
    if url:
        msg += f"\n{url}"

    return msg


def send_deal_alert_new(deal: dict[str, Any]) -> bool:
    """Send a deal alert if execution_gate allows it."""
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
