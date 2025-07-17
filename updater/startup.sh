#!/bin/bash
set -e

echo "Starting IP2Location Updater Service..."

# Check if main geolocation database exists
DB_PATH="/app/db/IP2LOCATION-LITE-DB1.BIN"
ASN_DB_PATH="/app/db/IP2LOCATION-LITE-ASN.BIN"
NEEDS_DOWNLOAD=false

if [ ! -f "$DB_PATH" ]; then
    echo "Main geolocation database not found"
    NEEDS_DOWNLOAD=true
fi

if [ ! -f "$ASN_DB_PATH" ]; then
    echo "ASN database not found"
    NEEDS_DOWNLOAD=true
fi

if [ "$NEEDS_DOWNLOAD" = true ]; then
    echo "Downloading missing databases..."
    cd /app
    
    # Download main database
    if [ ! -f "$DB_PATH" ]; then
        echo "Downloading main geolocation database..."
        python updater.py
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
        
        # Download ASN database
        curl -L -o "/downloads/${IP2LOCATION_ASN_DATABASE_CODE}.ZIP" "$ASN_URL"
        if [ $? -eq 0 ]; then
            echo "ASN database ZIP downloaded successfully"
            
            # Extract ASN database
            cd /downloads
            unzip -j "${IP2LOCATION_ASN_DATABASE_CODE}.ZIP" "*.BIN" -d /app/db/
            if [ $? -eq 0 ]; then
                # Rename to expected filename
                find /app/db/ -name "*ASN*.BIN" -exec mv {} "$ASN_DB_PATH" \;
                echo "ASN database extracted and installed successfully"
            else
                echo "Failed to extract ASN database"
            fi
            
            # Cleanup
            rm -f "/downloads/${IP2LOCATION_ASN_DATABASE_CODE}.ZIP"
        else
            echo "Failed to download ASN database"
        fi
    elif [ ! -f "$ASN_DB_PATH" ]; then
        echo "ASN database not found, but no token or ASN code configured - skipping ASN download"
    fi
else
    echo "All databases exist, skipping initial download"
fi

# Start cron for scheduled updates
echo "Starting cron daemon..."
exec cron -f 