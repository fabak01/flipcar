#!/bin/bash
# server_setup.sh — One-time setup for FlipCar on a fresh Ubuntu 22.04/24.04 server.
# Run as: bash scripts/server_setup.sh
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
echo "=== FlipCar Server Setup ==="
echo "App dir: $APP_DIR"

# --- System dependencies ---
echo "[1/5] Installing system packages..."
sudo apt-get update -qq
sudo apt-get install -y --no-install-recommends \
    python3 python3-pip python3-venv \
    git curl wget ca-certificates \
    libnss3 libatk-bridge2.0-0 libdrm2 libxkbcommon0 libgbm1 \
    libgtk-3-0 libxss1 libasound2 libxrandr2 libxfixes3 libxcomposite1 \
    libxdamage1 fonts-liberation libappindicator3-1 xdg-utils

# --- Python venv ---
echo "[2/5] Creating Python virtual environment..."
cd "$APP_DIR"
python3 -m venv .venv
.venv/bin/pip install --upgrade pip -q
.venv/bin/pip install -r requirements.txt -q

# --- Playwright browsers ---
echo "[3/5] Installing Playwright Chromium..."
.venv/bin/playwright install chromium
.venv/bin/playwright install-deps chromium

# --- Log directory ---
echo "[4/5] Creating logs directory..."
mkdir -p "$APP_DIR/logs"

# --- Cron wrapper executable ---
echo "[5/5] Making scripts executable..."
chmod +x "$APP_DIR/scripts/run_scheduled.sh"
chmod +x "$APP_DIR/scripts/push_cookies.sh"

echo ""
echo "=== Setup complete ==="
echo ""
echo "Next steps:"
echo "  1. Copy your .env file:    scp .env user@server:$APP_DIR/"
echo "  2. Push FINN cookies:      make push-cookies SERVER=user@server"
echo "  3. Test the run:           cd $APP_DIR && make smoke"
echo "  4. Install cron:           make cron-install"
echo ""
