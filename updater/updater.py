#!/usr/bin/env python3
"""
IP2Location Database Updater Service
Downloads and updates IP2Location LITE binary database daily
Binary format only for optimal performance
"""

import os
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
IP2LOCATION_DATABASE_PATH = os.getenv('IP2LOCATION_DATABASE_PATH', '/app/db/IP2LOCATION-LITE-DB11.BIN')
IP2LOCATION_DOWNLOAD_TOKEN = os.getenv('IP2LOCATION_DOWNLOAD_TOKEN')
IP2LOCATION_DATABASE_CODE = os.getenv('IP2LOCATION_DATABASE_CODE', 'DB11LITE')
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
            # LITE database URL
            return f"https://download.ip2location.com/lite/{IP2LOCATION_DATABASE_CODE}.BIN.ZIP"

    def download_database(self) -> Optional[Path]:
        """Download IP2Location LITE or commercial binary database."""
        url = self.get_download_url()
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
            
            # Move the new binary file to the target location
            bin_path.rename(target_path)
            logger.info(f"Binary database updated: {target_path}")
            
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
    
    def perform_update(self):
        """Perform complete database update"""
        logger.info("Starting IP2Location binary database update")
        
        try:
            # Download database
            zip_path = self.download_database()
            if not zip_path:
                logger.error("Failed to download database")
                return False
            
            # Create backup of current database
            if not self.backup_current_database():
                logger.warning("Backup failed, continuing with update...")
            
            # Extract binary database
            bin_path = self.extract_binary_from_zip(zip_path)
            if not bin_path:
                logger.error("Failed to extract binary file")
                return False
            
            # Update the database
            if self.update_binary_database(bin_path):
                logger.info("Binary database update completed successfully")
                
                # Log update information
                self.log_update_info()
                
                # Cleanup downloaded files
                try:
                    zip_path.unlink()
                    logger.info("Cleaned up temporary files")
                except Exception as e:
                    logger.warning(f"Failed to cleanup files: {e}")
                
                return True
            else:
                logger.error("Binary database update failed")
                return False
                
        except Exception as e:
            logger.error(f"Database update failed: {e}")
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