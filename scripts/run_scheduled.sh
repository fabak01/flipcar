#!/bin/bash
# run_scheduled.sh — Cron entrypoint for FlipCar live run.
# Handles logging, crash detection, and Telegram health alerts on failure.
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
LOG_DIR="$APP_DIR/logs"
TIMESTAMP=$(date +"%Y-%m-%d_%H-%M-%S")
LOG_FILE="$LOG_DIR/run_$TIMESTAMP.log"
PYTHON="$APP_DIR/.venv/bin/python"

mkdir -p "$LOG_DIR"

echo "=== FlipCar run started at $TIMESTAMP ===" | tee -a "$LOG_FILE"

# Run with unbuffered output, capture all output to log
cd "$APP_DIR"
if PYTHONUNBUFFERED=1 "$PYTHON" -m src.main --live >> "$LOG_FILE" 2>&1; then
    echo "=== Run finished OK at $(date +"%H:%M:%S") ===" | tee -a "$LOG_FILE"
else
    EXIT_CODE=$?
    echo "=== Run FAILED (exit $EXIT_CODE) at $(date +"%H:%M:%S") ===" | tee -a "$LOG_FILE"

    # Send Telegram health alert on crash
    TOKEN=$(grep TELEGRAM_BOT_TOKEN "$APP_DIR/.env" 2>/dev/null | cut -d= -f2)
    CHAT=$(grep TELEGRAM_CHAT_ID "$APP_DIR/.env" 2>/dev/null | cut -d= -f2)
    if [ -n "$TOKEN" ] && [ -n "$CHAT" ]; then
        LAST_LINES=$(tail -20 "$LOG_FILE" | tr '\n' '|')
        curl -s -X POST "https://api.telegram.org/bot${TOKEN}/sendMessage" \
            -d chat_id="$CHAT" \
            -d text="[FLIPCAR CRASH] Exit $EXIT_CODE at $TIMESTAMP. Check logs: $LOG_FILE" \
            > /dev/null
    fi
    exit $EXIT_CODE
fi

# Rotate logs: keep last 30 runs
cd "$LOG_DIR"
ls -t run_*.log 2>/dev/null | tail -n +31 | xargs rm -f 2>/dev/null || true
