#!/bin/bash
set -e

echo "Starting IP2Location Updater Service..."

# Check if main geolocation database exists
DB_PATH="/app/db/IP2LOCATION-LITE-DB1.BIN"
ASN_DB_PATH="/app/db/IP2LOCATION-LITE-ASN.BIN"

# Determine proxy database path based on PROXY_DATABASE_CODE
if [ ! -z "$PROXY_DATABASE_CODE" ] && [[ "$PROXY_DATABASE_CODE" == *"LITE"* ]]; then
    # Extract PX number from code like PX12LITECSVIPV6
    PX_CODE=$(echo "$PROXY_DATABASE_CODE" | sed 's/LITE.*//g' | sed 's/PX//g')
    if [[ "$PROXY_DATABASE_CODE" == *"IPV6"* ]]; then
        PROXY_DB_PATH="/app/db/IP2PROXY-LITE-PX${PX_CODE}.IPV6.CSV"
    else
        PROXY_DB_PATH="/app/db/IP2PROXY-LITE-PX${PX_CODE}.CSV"
    fi
else
    PROXY_DB_PATH="/app/db/IP2PROXY-LITE-PX1.CSV"
fi

NEEDS_DOWNLOAD=false

if [ ! -f "$DB_PATH" ]; then
    echo "Main geolocation database not found"
    NEEDS_DOWNLOAD=true
fi

if [ ! -f "$ASN_DB_PATH" ]; then
    echo "ASN database not found"
    NEEDS_DOWNLOAD=true
fi

if [ ! -f "$PROXY_DB_PATH" ] && [ "$ENABLE_PROXY_DETECTION" = "true" ]; then
    echo "Proxy database not found"
    NEEDS_DOWNLOAD=true
fi

if [ "$NEEDS_DOWNLOAD" = true ]; then
    echo "Downloading missing databases..."
    cd /app
    
    # Download main database
    if [ ! -f "$DB_PATH" ]; then
        echo "Downloading main geolocation database..."
        python3 updater.py
        if [ $? -eq 0 ]; then
            echo "Main database download completed successfully"
        else
            echo "Main database download failed"
        fi
    fi
    
    # Download ASN database if token is available
    if [ ! -f "$ASN_DB_PATH" ] && [ ! -z "$IP2LOCATION_DOWNLOAD_TOKEN" ] && [ ! -z "$IP2LOCATION_ASN_DATABASE_CODE" ]; then
        echo "Downloading ASN database..."
        ASN_URL="https://www.ip2location.com/download/?token=${IP2LOCATION_DOWNLOAD_TOKEN}&file=${IP2LOCATION_ASN_DATABASE_CODE}"
        ASN_ZIP_PATH="/downloads/${IP2LOCATION_ASN_DATABASE_CODE}.ZIP"
        
        # Download ASN database with better error handling
        echo "Downloading from: ${ASN_URL}"
        curl -L -f --connect-timeout 30 --max-time 600 -o "$ASN_ZIP_PATH" "$ASN_URL"
        CURL_EXIT_CODE=$?
        
        if [ $CURL_EXIT_CODE -eq 0 ] && [ -f "$ASN_ZIP_PATH" ]; then
            # Check if file was actually downloaded (not empty)
            FILE_SIZE=$(stat -c%s "$ASN_ZIP_PATH" 2>/dev/null || stat -f%z "$ASN_ZIP_PATH" 2>/dev/null || echo "0")
            echo "Downloaded file size: ${FILE_SIZE} bytes"
            
            if [ "$FILE_SIZE" -gt 1000 ]; then
                echo "ASN database ZIP downloaded successfully"
                
                # Verify ZIP file integrity
                if unzip -t "$ASN_ZIP_PATH" >/dev/null 2>&1; then
                    echo "ZIP file integrity verified"
                    
                    # Extract ASN database
                    cd /downloads
                    unzip -j "${IP2LOCATION_ASN_DATABASE_CODE}.ZIP" "*.BIN" -d /app/db/
                    if [ $? -eq 0 ]; then
                        # Rename to expected filename
                        find /app/db/ -name "*ASN*.BIN" -exec mv {} "$ASN_DB_PATH" \;
                        echo "ASN database extracted and installed successfully"
                        
                        # Verify the final file exists
                        if [ -f "$ASN_DB_PATH" ]; then
                            FINAL_SIZE=$(stat -c%s "$ASN_DB_PATH" 2>/dev/null || stat -f%z "$ASN_DB_PATH" 2>/dev/null || echo "0")
                            echo "ASN database installed successfully (${FINAL_SIZE} bytes)"
                        fi
                    else
                        echo "Failed to extract ASN database"
                    fi
                else
                    echo "Downloaded ZIP file is corrupted"
                    ls -la "$ASN_ZIP_PATH"
                    head -c 100 "$ASN_ZIP_PATH" | hexdump -C
                fi
                
                # Cleanup
                rm -f "$ASN_ZIP_PATH"
            else
                echo "Downloaded file is too small (${FILE_SIZE} bytes) - likely an error page"
                if [ -f "$ASN_ZIP_PATH" ]; then
                    echo "File content preview:"
                    head -n 5 "$ASN_ZIP_PATH" || true
                    rm -f "$ASN_ZIP_PATH"
                fi
            fi
        else
            echo "Failed to download ASN database (curl exit code: $CURL_EXIT_CODE)"
        fi
    elif [ ! -f "$ASN_DB_PATH" ]; then
        echo "ASN database not found, but no token or ASN code configured - skipping ASN download"
    fi
    
    # Download proxy database if missing and enabled
    if [ ! -f "$PROXY_DB_PATH" ] && [ "$ENABLE_PROXY_DETECTION" = "true" ]; then
        echo "Downloading proxy database..."
        echo "Expected proxy database path: $PROXY_DB_PATH"
        echo "Proxy database code: $PROXY_DATABASE_CODE"
        
        # Use the main updater.py which now handles proxy databases
        python3 updater.py
        
        if [ $? -eq 0 ]; then
            echo "Proxy database download completed successfully"
            if [ -f "$PROXY_DB_PATH" ]; then
                PROXY_SIZE=$(stat -c%s "$PROXY_DB_PATH" 2>/dev/null || stat -f%z "$PROXY_DB_PATH" 2>/dev/null || echo "0")
                echo "Proxy database installed successfully (${PROXY_SIZE} bytes)"
            else
                echo "Warning: Proxy database download reported success but file not found"
            fi
        else
            echo "Proxy database download failed"
        fi
    fi
else
    echo "All databases exist, skipping initial download"
fi

# Start scheduled updates without cron
echo "Starting scheduled update service..."
echo "Update schedule: ${UPDATE_SCHEDULE:-0 2 * * *} (Daily at 2 AM)"

# Function to run the updater
run_updater() {
    echo "Running scheduled database update..."
    cd /app
    python3 updater.py
    if [ $? -eq 0 ]; then
        echo "Scheduled update completed successfully"
    else
        echo "Scheduled update failed"
    fi
}

# Run initial update if needed
if [ "$NEEDS_DOWNLOAD" = true ]; then
    echo "Running initial update..."
    run_updater
fi

# Start the scheduling loop
while true; do
    # Sleep until next 2 AM
    now=$(date +%s)
    next_run=$(date -d "tomorrow 02:00:00" +%s)
    sleep_seconds=$((next_run - now))
    
    echo "Next update scheduled for $(date -d "tomorrow 02:00:00") (in ${sleep_seconds} seconds)"
    sleep $sleep_seconds
    
    run_updater
done 