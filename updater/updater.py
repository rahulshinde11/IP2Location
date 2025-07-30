#!/usr/bin/env python3
"""
IP2Location Database Updater Service
Downloads and updates IP2Location LITE binary database daily
Binary format only for optimal performance
"""

import os
import shutil
import zipfile
import hashlib
import logging
import requests
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

# Import IP2Location for binary database support
try:
    import IP2Location
    BINARY_SUPPORT = True
except ImportError:
    BINARY_SUPPORT = False
    logging.error("IP2Location library not available. This service requires the IP2Location library.")
    exit(1)

# Configure logging
logging.basicConfig(
    level=os.getenv('LOG_LEVEL', 'INFO'),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('/app/logs/updater.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Configuration
IP2LOCATION_DATABASE_PATH = os.getenv('IP2LOCATION_DATABASE_PATH', '/app/db/IP2LOCATION-LITE-DB1.BIN')
IP2LOCATION_DOWNLOAD_TOKEN = os.getenv('IP2LOCATION_DOWNLOAD_TOKEN')
IP2LOCATION_DATABASE_CODE = os.getenv('IP2LOCATION_DATABASE_CODE', 'DB1LITE')
PROXY_DATABASE_CODE = os.getenv('PROXY_DATABASE_CODE', 'PX1LITE')
ENABLE_PROXY_DETECTION = os.getenv('ENABLE_PROXY_DETECTION', 'false').lower() == 'true'
BACKUP_ENABLED = os.getenv('BACKUP_ENABLED', 'true').lower() == 'true'
BACKUP_RETENTION_DAYS = int(os.getenv('BACKUP_RETENTION_DAYS', '7'))

class IP2LocationUpdater:
    def __init__(self):
        self.download_dir = Path('/downloads')
        self.backup_dir = Path('/backups')
        self.db_dir = Path('/app/db')
        
        # Create necessary directories
        self.download_dir.mkdir(exist_ok=True)
        self.backup_dir.mkdir(exist_ok=True)
        self.db_dir.mkdir(exist_ok=True)
    
    def calculate_md5(self, file_path: Path) -> str:
        """Calculate MD5 hash of a file"""
        hash_md5 = hashlib.md5()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(4096), b""):
                hash_md5.update(chunk)
        return hash_md5.hexdigest()
    
    def get_download_url(self) -> str:
        """Construct the download URL based on token and database code."""
        if IP2LOCATION_DOWNLOAD_TOKEN:
            # Commercial database URL
            return f"https://www.ip2location.com/download?token={IP2LOCATION_DOWNLOAD_TOKEN}&file={IP2LOCATION_DATABASE_CODE}"
        else:
            # LITE database URL - convert DB11LITE to IP2LOCATION-LITE-DB11
            if IP2LOCATION_DATABASE_CODE.endswith('LITE'):
                # Extract the number from codes like DB11LITE, DB5LITE, etc.
                db_number = IP2LOCATION_DATABASE_CODE.replace('DB', '').replace('LITE', '')
                lite_filename = f"IP2LOCATION-LITE-DB{db_number}"
                return f"https://download.ip2location.com/lite/{lite_filename}.BIN.ZIP"
            else:
                # Fallback to original format for non-LITE databases
                return f"https://download.ip2location.com/lite/{IP2LOCATION_DATABASE_CODE}.BIN.ZIP"

    def download_database(self) -> Optional[Path]:
        """Download IP2Location LITE or commercial binary database."""
        url = self.get_download_url()
        
        # Create a proper filename for the download
        if IP2LOCATION_DOWNLOAD_TOKEN:
            filename = f"{IP2LOCATION_DATABASE_CODE}.ZIP"
        else:
            # For LITE databases, use the proper filename format
            if IP2LOCATION_DATABASE_CODE.endswith('LITE'):
                db_number = IP2LOCATION_DATABASE_CODE.replace('DB', '').replace('LITE', '')
                filename = f"IP2LOCATION-LITE-DB{db_number}.BIN.ZIP"
            else:
                filename = f"{IP2LOCATION_DATABASE_CODE}.ZIP"
        
        download_path = self.download_dir / filename
        
        logger.info(f"Downloading {IP2LOCATION_DATABASE_CODE} from {url.split('?')[0]}")
        
        try:
            response = requests.get(url, stream=True, timeout=300)
            response.raise_for_status()
            
            with open(download_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)
            
            file_size = download_path.stat().st_size
            logger.info(f"Downloaded {filename} ({file_size:,} bytes)")
            
            return download_path
            
        except requests.RequestException as e:
            logger.error(f"Failed to download database: {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error during download: {e}")
            return None
    
    def extract_binary_from_zip(self, zip_path: Path) -> Optional[Path]:
        """Extract binary file from ZIP archive"""
        try:
            with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                # Find the .BIN file
                bin_files = [f for f in zip_ref.namelist() if f.upper().endswith('.BIN')]
                if not bin_files:
                    logger.error("No .BIN file found in the ZIP archive")
                    return None
                
                bin_filename = bin_files[0]
                extracted_path = self.download_dir / bin_filename
                
                # Extract the binary file
                with zip_ref.open(bin_filename) as source, open(extracted_path, 'wb') as target:
                    target.write(source.read())
                
                logger.info(f"Extracted {bin_filename}")
                return extracted_path
                
        except zipfile.BadZipFile as e:
            logger.error(f"Invalid ZIP file: {e}")
            return None
        except Exception as e:
            logger.error(f"Failed to extract BIN from ZIP: {e}")
            return None
    
    def backup_current_database(self) -> bool:
        """Create backup of current binary database."""
        if not BACKUP_ENABLED:
            logger.info("Backup disabled, skipping...")
            return True
        
        try:
            binary_path = Path(IP2LOCATION_DATABASE_PATH)
            if binary_path.exists():
                timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
                backup_file = self.backup_dir / f"ip2location_binary_backup_{timestamp}.bin"
                backup_file.write_bytes(binary_path.read_bytes())
                logger.info(f"Binary database backed up: {backup_file}")
                
                # Clean up old backups
                self.cleanup_old_backups()
            else:
                logger.info("No existing database to backup")
            
            return True
            
        except Exception as e:
            logger.error(f"Backup failed: {e}")
            return False
    
    def cleanup_old_backups(self):
        """Remove backups older than retention period."""
        try:
            cutoff_date = datetime.now() - timedelta(days=BACKUP_RETENTION_DAYS)
            removed_count = 0
            
            for backup_file in self.backup_dir.glob("ip2location_binary_backup_*.bin"):
                file_time = datetime.fromtimestamp(backup_file.stat().st_mtime)
                if file_time < cutoff_date:
                    backup_file.unlink()
                    removed_count += 1
                    logger.debug(f"Removed old backup: {backup_file}")
            
            if removed_count > 0:
                logger.info(f"Cleaned up {removed_count} old backup(s)")
                    
        except Exception as e:
            logger.warning(f"Failed to cleanup old backups: {e}")
    
    def validate_binary_database(self, bin_path: Path) -> bool:
        """Validate binary database by testing a lookup."""
        try:
            # Test the binary database
            test_db = IP2Location.IP2Location(str(bin_path))
            result = test_db.get_all("8.8.8.8")
            
            if result.country_short == '-' or result.country_short == 'INVALID IP ADDRESS':
                logger.warning("Binary database validation: Test IP lookup returned no data")
            else:
                logger.info(f"Binary database validation successful: {result.country_short} ({result.country_long})")
            
            return True
            
        except Exception as e:
            logger.error(f"Binary database validation failed: {e}")
            return False
    
    def update_binary_database(self, bin_path: Path) -> bool:
        """Update binary database."""
        try:
            # Validate the new binary database
            if not self.validate_binary_database(bin_path):
                logger.error("New binary database failed validation")
                return False
            
            target_path = Path(IP2LOCATION_DATABASE_PATH)
            
            # Copy the new binary file to the target location (instead of move to avoid cross-device issues)
            shutil.copy2(str(bin_path), str(target_path))
            logger.info(f"Binary database updated: {target_path}")
            
            # Clean up the source file after successful copy
            try:
                bin_path.unlink()
                logger.info("Cleaned up temporary binary file")
            except Exception as e:
                logger.warning(f"Failed to cleanup temporary file: {e}")
            
            # Log database information
            try:
                file_size = target_path.stat().st_size
                file_md5 = self.calculate_md5(target_path)
                logger.info(f"Database file size: {file_size:,} bytes")
                logger.info(f"Database MD5 hash: {file_md5}")
            except Exception as e:
                logger.warning(f"Could not log database information: {e}")
            
            return True
            
        except Exception as e:
            logger.error(f"Failed to update binary database: {e}")
            return False
    
    def log_update_info(self) -> None:
        """Log update completion information."""
        try:
            timestamp = datetime.now().isoformat()
            update_info = {
                "timestamp": timestamp,
                "database_version": IP2LOCATION_DATABASE_CODE,
                "database_type": "binary",
                "status": "success"
            }
            
            # Log to a simple text file for tracking
            log_file = self.db_dir / "last_update.log"
            with open(log_file, 'w') as f:
                for key, value in update_info.items():
                    f.write(f"{key}: {value}\n")
            
            logger.info(f"Update information logged to {log_file}")
            
        except Exception as e:
            logger.warning(f"Failed to log update information: {e}")
    
    def get_proxy_download_url(self) -> str:
        """Construct the proxy database download URL"""
        if IP2LOCATION_DOWNLOAD_TOKEN:
            # Commercial proxy database URL
            return f"https://www.ip2location.com/download?token={IP2LOCATION_DOWNLOAD_TOKEN}&file={PROXY_DATABASE_CODE}"
        else:
            # LITE proxy database URL - handle various formats like PX1LITE, PX12LITECSVIPV6
            if 'LITE' in PROXY_DATABASE_CODE:
                # Extract the PX number from codes like PX1LITE, PX2LITE, PX12LITECSVIPV6
                code = PROXY_DATABASE_CODE
                
                # Remove known suffixes to get the base PX number
                code = code.replace('LITE', '').replace('CSV', '').replace('IPV6', '').replace('IPV4', '')
                px_number = code.replace('PX', '')
                
                # Determine if this is IPv6 database
                is_ipv6 = 'IPV6' in PROXY_DATABASE_CODE
                
                if is_ipv6:
                    lite_filename = f"IP2PROXY-LITE-PX{px_number}.IPV6"
                else:
                    lite_filename = f"IP2PROXY-LITE-PX{px_number}"
                
                return f"https://download.ip2location.com/lite/{lite_filename}.CSV.ZIP"
            else:
                # Fallback for non-LITE proxy databases
                return f"https://www.ip2location.com/download?token={IP2LOCATION_DOWNLOAD_TOKEN}&file={PROXY_DATABASE_CODE}"
    
    def download_proxy_database(self) -> Optional[Path]:
        """Download IP2Proxy LITE or commercial CSV database."""
        if not ENABLE_PROXY_DETECTION:
            logger.info("Proxy detection disabled, skipping proxy database download")
            return None
            
        url = self.get_proxy_download_url()
        
        # Create a proper filename for the download
        if IP2LOCATION_DOWNLOAD_TOKEN:
            filename = f"{PROXY_DATABASE_CODE}.ZIP"
        else:
            # For LITE proxy databases, use the proper filename format
            if 'LITE' in PROXY_DATABASE_CODE:
                code = PROXY_DATABASE_CODE
                code = code.replace('LITE', '').replace('CSV', '').replace('IPV6', '').replace('IPV4', '')
                px_number = code.replace('PX', '')
                
                is_ipv6 = 'IPV6' in PROXY_DATABASE_CODE
                
                if is_ipv6:
                    filename = f"IP2PROXY-LITE-PX{px_number}.IPV6.CSV.ZIP"
                else:
                    filename = f"IP2PROXY-LITE-PX{px_number}.CSV.ZIP"
            else:
                filename = f"{PROXY_DATABASE_CODE}.ZIP"
        
        download_path = self.download_dir / filename
        
        logger.info(f"Downloading {PROXY_DATABASE_CODE} from {url.split('?')[0]}")
        
        try:
            response = requests.get(url, stream=True, timeout=300)
            response.raise_for_status()
            
            with open(download_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)
            
            file_size = download_path.stat().st_size
            logger.info(f"Downloaded proxy database {filename} ({file_size:,} bytes)")
            
            return download_path
            
        except requests.RequestException as e:
            logger.error(f"Failed to download proxy database: {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error during proxy database download: {e}")
            return None
    
    def extract_csv_from_zip(self, zip_path: Path) -> Optional[Path]:
        """Extract CSV file from ZIP archive"""
        try:
            with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                # Find the .CSV file
                csv_files = [f for f in zip_ref.namelist() if f.upper().endswith('.CSV')]
                if not csv_files:
                    logger.error("No .CSV file found in the ZIP archive")
                    return None
                
                csv_filename = csv_files[0]
                extracted_path = self.download_dir / csv_filename
                
                # Extract the CSV file
                with zip_ref.open(csv_filename) as source, open(extracted_path, 'wb') as target:
                    target.write(source.read())
                
                logger.info(f"Extracted {csv_filename}")
                return extracted_path
                
        except zipfile.BadZipFile as e:
            logger.error(f"Invalid ZIP file: {e}")
            return None
        except Exception as e:
            logger.error(f"Failed to extract CSV from ZIP: {e}")
            return None
    
    def get_proxy_database_path(self) -> Path:
        """Get the target path for the proxy CSV database"""
        if 'LITE' in PROXY_DATABASE_CODE:
            code = PROXY_DATABASE_CODE
            code = code.replace('LITE', '').replace('CSV', '').replace('IPV6', '').replace('IPV4', '')
            px_number = code.replace('PX', '')
            
            is_ipv6 = 'IPV6' in PROXY_DATABASE_CODE
            
            if is_ipv6:
                return self.db_dir / f"IP2PROXY-LITE-PX{px_number}.IPV6.CSV"
            else:
                return self.db_dir / f"IP2PROXY-LITE-PX{px_number}.CSV"
        else:
            return self.db_dir / f"{PROXY_DATABASE_CODE}.CSV"
    
    def backup_current_proxy_database(self) -> bool:
        """Create backup of current proxy CSV database."""
        if not BACKUP_ENABLED:
            return True
            
        proxy_db_path = self.get_proxy_database_path()
        if not proxy_db_path.exists():
            logger.info("No existing proxy database to backup")
            return True
        
        try:
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            backup_filename = f"{proxy_db_path.stem}_backup_{timestamp}.csv"
            backup_path = self.backup_dir / backup_filename
            
            shutil.copy2(proxy_db_path, backup_path)
            logger.info(f"Proxy database backed up to: {backup_path}")
            
            return True
            
        except Exception as e:
            logger.error(f"Failed to backup proxy database: {e}")
            return False
    
    def update_proxy_database(self, csv_path: Path) -> bool:
        """Update the proxy CSV database file."""
        try:
            target_path = self.get_proxy_database_path()
            
            # Validate CSV file
            try:
                with open(csv_path, 'r', encoding='utf-8') as f:
                    import csv as csv_module
                    reader = csv_module.reader(f)
                    headers = next(reader, [])
                    first_row = next(reader, [])
                    
                    if len(headers) < 4 or len(first_row) < 4:
                        logger.error("Invalid proxy CSV format - insufficient columns")
                        return False
                    
                    logger.info(f"Proxy CSV validation passed: {len(headers)} columns detected")
                    
            except Exception as e:
                logger.error(f"Proxy CSV validation failed: {e}")
                return False
            
            # Copy the file to the target location (instead of move to avoid cross-device issues)
            shutil.copy2(str(csv_path), str(target_path))
            
            # Set proper permissions
            target_path.chmod(0o644)
            
            # Clean up the source file after successful copy
            try:
                csv_path.unlink()
                logger.info("Cleaned up temporary CSV file")
            except Exception as e:
                logger.warning(f"Failed to cleanup temporary CSV file: {e}")
            
            file_size = target_path.stat().st_size
            logger.info(f"Proxy database updated: {target_path} ({file_size:,} bytes)")
            
            return True
            
        except Exception as e:
            logger.error(f"Failed to update proxy database: {e}")
            return False
    
    def perform_proxy_update(self) -> bool:
        """Perform complete proxy database update"""
        if not ENABLE_PROXY_DETECTION:
            logger.info("Proxy detection disabled, skipping proxy database update")
            return True
            
        logger.info("Starting proxy database update")
        
        try:
            # Download proxy database
            zip_path = self.download_proxy_database()
            if not zip_path:
                logger.error("Failed to download proxy database")
                return False
            
            # Create backup of current proxy database
            if not self.backup_current_proxy_database():
                logger.warning("Proxy backup failed, continuing with update...")
            
            # Extract CSV database
            csv_path = self.extract_csv_from_zip(zip_path)
            if not csv_path:
                logger.error("Failed to extract CSV file")
                return False
            
            # Update the proxy database
            if self.update_proxy_database(csv_path):
                logger.info("Proxy database update completed successfully")
                
                # Cleanup downloaded files
                try:
                    zip_path.unlink()
                    logger.info("Cleaned up proxy temporary files")
                except Exception as e:
                    logger.warning(f"Failed to cleanup proxy files: {e}")
                
                return True
            else:
                logger.error("Proxy database update failed")
                return False
                
        except Exception as e:
            logger.error(f"Proxy database update failed: {e}")
            return False
    
    def perform_update(self):
        """Perform complete database update (geolocation + proxy)"""
        logger.info("Starting database update process")
        
        geo_success = True
        proxy_success = True
        
        # Update geolocation database
        try:
            logger.info("Starting IP2Location binary database update")
            
            # Download database
            zip_path = self.download_database()
            if not zip_path:
                logger.error("Failed to download geolocation database")
                geo_success = False
            else:
                # Create backup of current database
                if not self.backup_current_database():
                    logger.warning("Geolocation backup failed, continuing with update...")
                
                # Extract binary database
                bin_path = self.extract_binary_from_zip(zip_path)
                if not bin_path:
                    logger.error("Failed to extract binary file")
                    geo_success = False
                else:
                    # Update the database
                    if self.update_binary_database(bin_path):
                        logger.info("Geolocation database update completed successfully")
                        
                        # Log update information
                        self.log_update_info()
                        
                        # Cleanup downloaded files
                        try:
                            zip_path.unlink()
                            logger.info("Cleaned up geolocation temporary files")
                        except Exception as e:
                            logger.warning(f"Failed to cleanup geolocation files: {e}")
                    else:
                        logger.error("Geolocation database update failed")
                        geo_success = False
                        
        except Exception as e:
            logger.error(f"Geolocation database update failed: {e}")
            geo_success = False
        
        # Update proxy database (if enabled)
        if ENABLE_PROXY_DETECTION:
            try:
                proxy_success = self.perform_proxy_update()
            except Exception as e:
                logger.error(f"Proxy database update failed: {e}")
                proxy_success = False
        else:
            logger.info("Proxy detection disabled, skipping proxy database update")
        
        # Determine overall success
        if geo_success and proxy_success:
            logger.info("All database updates completed successfully")
            return True
        elif geo_success:
            logger.warning("Geolocation database updated, but proxy database update failed")
            return True  # Still consider success if main database updated
        else:
            logger.error("Database update process failed")
            return False

def main():
    """Main entry point for the updater script."""
    logger.info("IP2Location Binary Database Updater starting...")

    if not BINARY_SUPPORT:
        logger.error("IP2Location library not available. Install with: pip install IP2Location")
        sys.exit(1)

    updater = IP2LocationUpdater()
    
    # Perform the update
    if updater.perform_update():
        logger.info("Update process finished successfully.")
        sys.exit(0)
    else:
        logger.error("Update process failed.")
        sys.exit(1)

if __name__ == '__main__':
    main() 