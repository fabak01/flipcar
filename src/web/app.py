"""FlipCar deal tracker web dashboard.

Run with:
    .venv/bin/python -m src.web.app
    (or set WEB_PORT env var to change port, default 5000)
"""

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT / ".env")

from flask import Flask, redirect, render_template, request, url_for

from src.db.supabase_client import (
    get_deal_detail,
    get_deals_for_dashboard,
    upsert_deal_status,
)

app = Flask(__name__, template_folder="templates")
app.secret_key = os.getenv("FLASK_SECRET_KEY", "flipcar-dashboard-dev")


# ── Template helpers ─────────────────────────────────────────────────────────

def _fmt(n) -> str:
    """Format number with Norwegian space thousands separator."""
    if n is None:
        return "N/A"
    try:
        return f"{int(round(float(n))):,}".replace(",", "\u202f")
    except (TypeError, ValueError):
        return str(n)


def _pct(n) -> str:
    if n is None:
        return "N/A"
    try:
        return f"{float(n) * 100:.1f}%"
    except (TypeError, ValueError):
        return str(n)


def _ago(ts_str: str) -> str:
    """Return a human-readable 'X days ago' string from an ISO timestamp."""
    if not ts_str:
        return ""
    try:
        ts = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00"))
        delta = datetime.now(timezone.utc) - ts
        days = delta.days
        if days == 0:
            hours = delta.seconds // 3600
            return f"{hours}t siden" if hours > 0 else "akkurat nå"
        if days == 1:
            return "i går"
        return f"{days}d siden"
    except Exception:
        return ""


app.jinja_env.globals["fmt"] = _fmt
app.jinja_env.globals["pct"] = _pct
app.jinja_env.globals["ago"] = _ago

STATUS_LABELS = {
    "new": "Ny",
    "contacted": "Kontaktet",
    "bid_placed": "Bud lagt",
    "won": "Vunnet",
    "lost": "Tapt",
    "skipped": "Hoppet over",
}

ADJ_LABELS = {
    "eu_kontroll_godkjent_fersk": "EU fersk",
    "eu_kontroll_godkjent": "EU gyldig",
    "eu_kontroll_forfalt": "EU forfalt",
    "eu_kontroll_snart_forfalt": "EU snart forfalt",
    "eu_kontroll_nær_forfall": "EU nær forfall",
    "eu_kontroll_ikke_nevnt": "EU ikke nevnt",
    "dekk_nye_vinter_og_sommer": "Nye dekk (vinter+sommer)",
    "dekk_nye_vinterdekk": "Nye vinterdekk",
    "dekk_slitte_dekk": "Slitte dekk",
    "dekk_mangler_sett": "Mangler dekksett",
    "service_full_historikk": "Full servicehistorikk",
    "service_delvis": "Delvis service",
    "service_ingen": "Ingen servicehistorikk",
    "service_forfalt": "Service forfalt",
    "batteri_soh_over_90": "SOH >90%",
    "batteri_soh_85_90": "SOH 85–90%",
    "batteri_soh_80_85": "SOH 80–85%",
    "batteri_soh_under_80": "SOH <80%",
    "batteri_soh_ikke_oppgitt": "SOH ikke oppgitt",
    "batteri_soh_garanti_gjenstar": "Batterigaranti gjenstår",
    "skade_smaskader": "Småskader",
    "skade_bulk_riper": "Bulk/riper",
    "skade_rust": "Rust",
    "skade_selges_som_den_er": "Selges som den er",
}

app.jinja_env.globals["STATUS_LABELS"] = STATUS_LABELS
app.jinja_env.globals["ADJ_LABELS"] = ADJ_LABELS


# ── Routes ───────────────────────────────────────────────────────────────────

@app.route("/")
def deals_list():
    label_filter = request.args.get("label", "")
    status_filter = request.args.get("status", "")

    deals = get_deals_for_dashboard(limit=300)

    if label_filter:
        deals = [d for d in deals if d.get("classification_label", "") == label_filter]
    if status_filter:
        deals = [d for d in deals if (d.get("status_data") or {}).get("status", "new") == status_filter]

    # Sort: active statuses first, then by spread desc
    status_order = {"new": 0, "contacted": 1, "bid_placed": 2, "won": 3, "lost": 4, "skipped": 5}
    deals.sort(
        key=lambda d: (
            status_order.get((d.get("status_data") or {}).get("status", "new"), 9),
            -(d.get("spread_ask_pct") or -999),
        )
    )

    return render_template(
        "deals.html",
        deals=deals,
        label_filter=label_filter,
        status_filter=status_filter,
        status_labels=STATUS_LABELS,
        total=len(deals),
    )


@app.route("/deal/<listing_id>")
def deal_detail(listing_id: str):
    deal = get_deal_detail(listing_id)
    if not deal:
        return "<h1>Deal ikke funnet</h1>", 404
    return render_template("deal.html", deal=deal, status_labels=STATUS_LABELS, adj_labels=ADJ_LABELS)


@app.route("/deal/<listing_id>/status", methods=["POST"])
def update_status(listing_id: str):
    status = request.form.get("status", "").strip()
    bid_amount_raw = request.form.get("bid_amount", "").strip()
    notes = request.form.get("notes", "").strip()

    fields: dict = {}
    if status:
        fields["status"] = status
        if status == "bid_placed" and not bid_amount_raw:
            pass  # allow without amount
        if status == "bid_placed":
            fields["bid_at"] = datetime.now(timezone.utc).isoformat()
    if bid_amount_raw:
        try:
            fields["bid_amount"] = int(bid_amount_raw.replace("\u202f", "").replace(" ", "").replace(",", ""))
        except ValueError:
            pass
    if notes:
        fields["notes"] = notes

    if fields:
        upsert_deal_status(listing_id, **fields)

    return redirect(url_for("deal_detail", listing_id=listing_id))


if __name__ == "__main__":
    port = int(os.getenv("WEB_PORT", "5000"))
    debug = os.getenv("FLASK_DEBUG", "1") == "1"
    print(f"FlipCar dashboard → http://localhost:{port}")
    app.run(host="0.0.0.0", port=port, debug=debug)
