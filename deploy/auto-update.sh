#!/bin/bash
# Checks GitHub for new commits and hot-reloads the weather station if found.
# Run by auto-update.timer every 5 minutes.

set -euo pipefail

REPO_DIR="/home/fiwi42/weather_station"
SERVICE="weather-station"
LOG="$REPO_DIR/update.log"

cd "$REPO_DIR"

git fetch origin --quiet

LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse origin/main)

if [ "$LOCAL" = "$REMOTE" ]; then
    exit 0
fi

git pull origin main --quiet
sudo systemctl restart "$SERVICE"

echo "$(date -Iseconds)  updated $(git rev-parse --short "$LOCAL")→$(git rev-parse --short HEAD)" >> "$LOG"
