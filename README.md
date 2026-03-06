# FlipCar - Car Flip Underwriting Engine

Phase 1: Scrape bruktbilannonser fra FINN.no, beregn Fair Market Value (FMV), og finn underprisede biler.

## Oppsett

### 1. Installer avhengigheter

```bash
pip install -r requirements.txt
```

### 2. Konfigurer miljoevariabler

Kopier `.env.example` til `.env` og fyll inn verdiene:

```bash
cp .env.example .env
```

Noedvendige variabler:
- `SUPABASE_URL` - Supabase prosjekt-URL
- `SUPABASE_ANON_KEY` - Supabase anon key
- `SUPABASE_SERVICE_KEY` - Supabase service key
- `TELEGRAM_BOT_TOKEN` - Telegram bot token (fra @BotFather)
- `TELEGRAM_CHAT_ID` - Telegram chat ID for varsler
- `SVV_API_KEY` - Statens vegvesen API-noekkel

### 3. Sett opp database

Kjoer SQL fra `src/db/supabase_client.py` (`CREATE_TABLES_SQL`) i Supabase SQL-editoren.

### 4. Kjoer pipelinen

```bash
python -m src.main
```

## Arkitektur

```
config/          - YAML-konfigurasjon (modeller, parametere, patterns)
src/scraper/     - FINN.no scraper + SVV API oppslag
src/engine/      - Underwriting-motor (comps, FMV, reparasjoner, profitt)
src/output/      - Telegram-varsler og fil-output
src/db/          - Supabase database-operasjoner
tests/           - Tester
```

## Pipeline

1. Scrape alle konfigurerte bilmodeller fra FINN.no
2. Normaliser varianter og beregn datakvalitet
3. For hver annonse:
   - Finn sammenlignbare biler (comps) i 3 tiers
   - Beregn FMV med transaksjonspris-korreksjon
   - Juster FMV for dekk, EU, service, utstyr, skader
   - Estimer reparasjonskostnader (tekst + modellspesifikke)
   - Beregn salgstid, kapitalkostnad, profitt (bull/base/bear)
   - Beregn makspris (MPP)
   - Klassifiser deal
4. Send Telegram-varsel for gode deals
5. Skriv deals.jsonl og deals.csv

## Tester

```bash
pytest tests/ -v
```

## Konfigurasjon

Alle parametere er i `config/params.yaml`. Ingen hardkodede verdier i koden.
Bilmodeller konfigureres i `config/models.yaml`.
