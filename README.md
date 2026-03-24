# FlipCar — Practical Used Car Flip Radar for FINN.no

FlipCar is a **Pristips-first** deal radar that scrapes FINN.no, values listings against Pristips market data, and classifies them by actionability for car flipping.

## How It Works

```
Scrape FINN -> Pristips valuation -> AI text analysis -> Adjustments -> Spread -> Action label
```

### Core Model

1. **Anchor**: FINN Pristips is the ONLY production market anchor. No Pristips = no underwriting.
2. **Adjusted market value**: `V_adj = V_anchor + Adj_pos - Adj_neg - Rep_base`
3. **Spread**: `spread = (V_adj - price) / V_adj`
4. **Action label**: Based on spread thresholds

Comps are diagnostics only — never used as a production fallback.

### Action Labels

| Label | Meaning | Spread threshold |
|-------|---------|-----------------|
| **CALL_NOW** | Strong buy — call immediately | spread_ask >= 10% |
| **MESSAGE** | Worth negotiating | spread_bid >= 6% and spread_ask >= 4% |
| **WATCH** | Monitor for price drop | spread_bid >= 2% |
| **PASS** | Skip | Below thresholds or red flag |

### Execution Gate

| Gate | Meaning |
|------|---------|
| **SEND** | Telegram alert sent (AI available) |
| **SEND_NO_AI** | Telegram alert sent (AI unavailable — rule-based) |
| **NO_PRISTIPS** | No Pristips anchor — cannot underwrite |
| **BLOCKED** | Not actionable enough to alert |

### AI Text Analysis

Uses **gpt-4o-mini** to analyze Norwegian listing text. Extracts:
- Positive/negative signals
- Missing information to ask seller
- Seller motivation score (1-5)
- Hard red flags (taxi, accident, etc.)

**AI enriches output but never blocks classification.** If AI fails:
- `ai_status` shows what happened (`fallback_no_api_key`, `fallback_runtime_error`, etc.)
- Classification proceeds using rule-based analysis
- Telegram still sends with "AI unavailable" note

### ai_status values

| Value | Meaning |
|-------|---------|
| `ok` | AI analysis completed successfully |
| `short_text` | Listing text too short (<50 chars) |
| `fallback_no_api_key` | OPENAI_API_KEY not set |
| `fallback_import_error` | OpenAI package import failed |
| `fallback_runtime_error` | API call or JSON parse failed |

### Cookie Refresh

Pristips requires FINN session cookies:
```bash
python scripts/get_finn_cookies.py
# Log in to FINN in the browser that opens
# Test: python scripts/run_live_get_pristips.py EC60771 72123
```

## Running

```bash
# Smoke test (3 listings, no Telegram)
python -m src.main --smoke

# Smoke test with specific model
python -m src.main --smoke --model "Tesla Model 3"

# Dry run (all listings, CSV/JSONL output, no Telegram)
python -m src.main --dry-run

# Dry run with limit
python -m src.main --dry-run --limit 20

# Live (full run, Telegram enabled)
python -m src.main --live

# Live with model filter
python -m src.main --live --model "Tesla Model 3"
```

## Output

- `deals.csv` — Action-oriented spreadsheet sorted by spread
- `deals.jsonl` — Full audit trail with all detail
- Telegram — Clean alerts for CALL_NOW and MESSAGE deals

## Configuration

| File | Purpose |
|------|---------|
| `config/models.yaml` | Which car models to scrape |
| `config/params.yaml` | All thresholds and parameters |
| `config/variant_aliases.yaml` | Variant name normalization |
| `config/spec_adjustments.yaml` | Equipment value adjustments (SOLE owner) |
| `config/issue_catalog.yaml` | Condition/damage adjustments |
| `config/model_issues.yaml` | Model-specific known issues |
