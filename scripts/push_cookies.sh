#!/bin/bash
# push_cookies.sh — Push fresh FINN cookies from local machine to server.
#
# FINN cookies expire every 2-4 weeks. When they do:
#   1. Run locally: python scripts/get_finn_cookies.py
#   2. Then run:    make push-cookies SERVER=user@your-server
#
# Usage: bash scripts/push_cookies.sh user@your-server [remote_path]
set -euo pipefail

SERVER="${1:-}"
REMOTE_PATH="${2:-~/flipcar-clean}"
LOCAL_COOKIE="$(cd "$(dirname "$0")/.." && pwd)/.finn_cookies.json"

if [ -z "$SERVER" ]; then
    echo "Usage: $0 user@server [remote_path]"
    echo "Example: $0 root@1.2.3.4 ~/flipcar-clean"
    exit 1
fi

if [ ! -f "$LOCAL_COOKIE" ]; then
    echo "ERROR: $LOCAL_COOKIE not found."
    echo "Run first: python scripts/get_finn_cookies.py"
    exit 1
fi

AGE_DAYS=$(( ($(date +%s) - $(stat -f %m "$LOCAL_COOKIE" 2>/dev/null || stat -c %Y "$LOCAL_COOKIE")) / 86400 ))
echo "Cookie file age: ${AGE_DAYS} days"
if [ "$AGE_DAYS" -gt 20 ]; then
    echo "WARNING: cookies are ${AGE_DAYS} days old — consider refreshing first."
fi

echo "Pushing .finn_cookies.json to $SERVER:$REMOTE_PATH/ ..."
scp "$LOCAL_COOKIE" "$SERVER:$REMOTE_PATH/.finn_cookies.json"
echo "Done. Verifying..."
ssh "$SERVER" "ls -lh $REMOTE_PATH/.finn_cookies.json"
