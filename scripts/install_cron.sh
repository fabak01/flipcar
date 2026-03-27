#!/bin/bash
# install_cron.sh — Install the FlipCar cron job on the server.
# Runs at 07:15 and 18:15 Oslo time every day.
# Run as: bash scripts/install_cron.sh
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPT="$APP_DIR/scripts/run_scheduled.sh"
CRON_LOG="$APP_DIR/logs/cron.log"

chmod +x "$SCRIPT"
mkdir -p "$APP_DIR/logs"

# Remove any existing FlipCar cron entries, add fresh ones
TMPFILE=$(mktemp)
crontab -l 2>/dev/null | grep -v "flipcar\|run_scheduled" > "$TMPFILE" || true

cat >> "$TMPFILE" << EOF

# FlipCar — installed by install_cron.sh on $(date +"%Y-%m-%d")
15 7  * * * TZ=Europe/Oslo bash $SCRIPT >> $CRON_LOG 2>&1
15 18 * * * TZ=Europe/Oslo bash $SCRIPT >> $CRON_LOG 2>&1
EOF

crontab "$TMPFILE"
rm "$TMPFILE"

echo "Cron installed. Current crontab:"
crontab -l | grep -A1 -B1 "flipcar\|run_scheduled"
echo ""
echo "FlipCar will run at 07:15 and 18:15 Oslo time every day."
echo "Logs: $CRON_LOG"
echo "To remove: crontab -e"
