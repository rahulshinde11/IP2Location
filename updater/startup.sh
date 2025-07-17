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
else
    echo "All databases exist, skipping initial download"
fi

# Start cron for scheduled updates
echo "Starting cron daemon..."
exec cron -f 