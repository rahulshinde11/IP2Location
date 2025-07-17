#!/bin/bash
set -e

echo "Starting IP2Location Updater Service..."

# Check if database exists, if not, run updater immediately
DB_PATH="/app/db/IP2LOCATION-LITE-DB1.BIN"
if [ ! -f "$DB_PATH" ]; then
    echo "Database not found, downloading immediately..."
    cd /app
    python updater.py
    if [ $? -eq 0 ]; then
        echo "Initial database download completed successfully"
    else
        echo "Initial database download failed"
    fi
else
    echo "Database exists, skipping initial download"
fi

# Start cron for scheduled updates
echo "Starting cron daemon..."
exec cron -f 