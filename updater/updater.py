#!/usr/bin/env python3
"""
IP2Location Database Updater Service

Downloads and refreshes three databases on a schedule:
  - Geolocation  (IP2Location LITE, binary .BIN)
  - ASN          (IP2Location LITE ASN, binary .BIN)
  - Proxy        (IP2Proxy LITE, CSV)

Run modes:
  python3 updater.py            -> run a single update of all enabled databases, then exit
  python3 updater.py --loop     -> bootstrap any missing databases, then run on UPDATE_SCHEDULE forever
"""

import os
import sys
import time
import json
import shutil
import zipfile
import hashlib
import logging
import argparse
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

import requests
from croniter import croniter

# Import IP2Location for binary database validation
try:
    import IP2Location
    BINARY_SUPPORT = True
except ImportError:
    BINARY_SUPPORT = False
    logging.error("IP2Location library not available. This service requires the IP2Location library.")
    sys.exit(1)

# Configure logging
log_handlers = [logging.StreamHandler()]
try:
    os.makedirs('/app/logs', exist_ok=True)
    log_handlers.append(logging.FileHandler('/app/logs/updater.log'))
except (PermissionError, OSError) as e:
    print(f"Warning: Could not create log file, using console logging only: {e}")

logging.basicConfig(
    level=os.getenv('LOG_LEVEL', 'INFO'),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=log_handlers
)
logger = logging.getLogger(__name__)

# Configuration
IP2LOCATION_DOWNLOAD_TOKEN = os.getenv('IP2LOCATION_DOWNLOAD_TOKEN')

GEO_DATABASE_CODE = os.getenv('IP2LOCATION_DATABASE_CODE', 'DB1LITE')
GEO_DATABASE_PATH = os.getenv('IP2LOCATION_DATABASE_PATH', '/app/db/IP2LOCATION-LITE-DB1.BIN')

ASN_DATABASE_CODE = os.getenv('IP2LOCATION_ASN_DATABASE_CODE')
ASN_DATABASE_PATH = os.getenv('ASN_DATABASE_PATH', '/app/db/IP2LOCATION-LITE-ASN.BIN')

PROXY_DATABASE_CODE = os.getenv('PROXY_DATABASE_CODE', 'PX1LITE')
PROXY_DATABASE_PATH = os.getenv('PROXY_DATABASE_PATH')  # optional explicit override

ENABLE_PROXY_DETECTION = os.getenv('ENABLE_PROXY_DETECTION', 'false').lower() == 'true'
BACKUP_ENABLED = os.getenv('BACKUP_ENABLED', 'true').lower() == 'true'
BACKUP_RETENTION_DAYS = int(os.getenv('BACKUP_RETENTION_DAYS', '7'))
UPDATE_SCHEDULE = os.getenv('UPDATE_SCHEDULE', '0 2 * * *')


class IP2LocationUpdater:
    def __init__(self):
        self.download_dir = Path('/downloads')
        self.backup_dir = Path('/backups')
        self.db_dir = Path('/app/db')

        for d in (self.download_dir, self.backup_dir, self.db_dir):
            d.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ #
    # URL / filename resolution
    # ------------------------------------------------------------------ #
    @staticmethod
    def _lite_filename(code: str) -> str:
        """Build the public LITE download filename for a database code.

        Handles BIN vs CSV and IPv4 vs IPv6 combined datasets, e.g.
          DB11LITEBINIPV6  -> IP2LOCATION-LITE-DB11.IPV6.BIN.ZIP
          DBASNLITEBINIPV6 -> IP2LOCATION-LITE-ASN.IPV6.BIN.ZIP
          PX12LITECSVIPV6  -> IP2PROXY-LITE-PX12.IPV6.CSV.ZIP
          DB11LITE         -> IP2LOCATION-LITE-DB11.BIN.ZIP
        """
        c = code.upper()
        is_csv = 'CSV' in c
        is_ipv6 = 'IPV6' in c

        base = c
        for token in ('LITE', 'CSV', 'BIN', 'IPV6', 'IPV4'):
            base = base.replace(token, '')

        if base.startswith('DBASN'):
            name = 'IP2LOCATION-LITE-ASN'
        elif base.startswith('DB'):
            name = f'IP2LOCATION-LITE-DB{base[2:]}'
        elif base.startswith('PX'):
            name = f'IP2PROXY-LITE-PX{base[2:]}'
        else:
            name = base

        if is_ipv6:
            name += '.IPV6'
        name += '.CSV.ZIP' if is_csv else '.BIN.ZIP'
        return name

    def _resolve_download(self, code: str) -> Tuple[str, str]:
        """Return (url, local_zip_filename) for a database code.

        With a token, use the authenticated downloader (works for every code,
        including the *IPV6 LITE codes). Without a token, fall back to the
        public LITE download host.
        """
        if IP2LOCATION_DOWNLOAD_TOKEN:
            url = f"https://www.ip2location.com/download?token={IP2LOCATION_DOWNLOAD_TOKEN}&file={code}"
            return url, f"{code}.ZIP"
        filename = self._lite_filename(code)
        return f"https://download.ip2location.com/lite/{filename}", filename

    # ------------------------------------------------------------------ #
    # Generic helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def calculate_md5(file_path: Path) -> str:
        hash_md5 = hashlib.md5()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                hash_md5.update(chunk)
        return hash_md5.hexdigest()

    def download(self, code: str, label: str) -> Optional[Path]:
        """Download the ZIP for a database code into the downloads dir."""
        url, zip_name = self._resolve_download(code)
        dest = self.download_dir / zip_name
        logger.info(f"Downloading {label} ({code}) from {url.split('?')[0]}")
        try:
            with requests.get(url, stream=True, timeout=600) as response:
                response.raise_for_status()
                with open(dest, 'wb') as f:
                    for chunk in response.iter_content(chunk_size=8192):
                        f.write(chunk)
            size = dest.stat().st_size
            # A few hundred bytes almost always means an HTML error/quota page.
            if size < 1024:
                logger.error(f"{label} download is suspiciously small ({size} bytes) - likely an error page, not a database")
                dest.unlink(missing_ok=True)
                return None
            logger.info(f"Downloaded {label}: {zip_name} ({size:,} bytes)")
            return dest
        except requests.RequestException as e:
            logger.error(f"Failed to download {label}: {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error downloading {label}: {e}")
            return None

    def extract_member(self, zip_path: Path, suffix: str) -> Optional[Path]:
        """Extract the first archive member whose name ends with `suffix` (.BIN/.CSV)."""
        try:
            with zipfile.ZipFile(zip_path, 'r') as zf:
                members = [m for m in zf.namelist() if m.upper().endswith(suffix.upper())]
                if not members:
                    logger.error(f"No {suffix} file found inside {zip_path.name}")
                    return None
                member = members[0]
                extracted = self.download_dir / Path(member).name
                with zf.open(member) as src, open(extracted, 'wb') as dst:
                    shutil.copyfileobj(src, dst)
                logger.info(f"Extracted {member}")
                return extracted
        except zipfile.BadZipFile as e:
            logger.error(f"Invalid ZIP file {zip_path.name}: {e}")
            return None
        except Exception as e:
            logger.error(f"Failed to extract {suffix} from {zip_path.name}: {e}")
            return None

    def backup_file(self, source: Path, tag: str) -> None:
        """Copy the current database aside before overwriting it."""
        if not BACKUP_ENABLED or not source.exists():
            return
        try:
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            backup_path = self.backup_dir / f"{tag}_backup_{timestamp}{source.suffix}"
            shutil.copy2(source, backup_path)
            logger.info(f"Backed up {source.name} -> {backup_path.name}")
            self.cleanup_old_backups()
        except Exception as e:
            logger.warning(f"Backup of {source.name} failed (continuing): {e}")

    def cleanup_old_backups(self) -> None:
        try:
            cutoff = time.time() - BACKUP_RETENTION_DAYS * 86400
            removed = 0
            for backup in self.backup_dir.glob("*_backup_*"):
                if backup.stat().st_mtime < cutoff:
                    backup.unlink()
                    removed += 1
            if removed:
                logger.info(f"Cleaned up {removed} old backup(s)")
        except Exception as e:
            logger.warning(f"Failed to cleanup old backups: {e}")

    @staticmethod
    def validate_bin(bin_path: Path) -> bool:
        """Confirm a binary database opens and answers a lookup."""
        try:
            db = IP2Location.IP2Location(str(bin_path))
            db.get_all("8.8.8.8")
            return True
        except Exception as e:
            logger.error(f"Binary database validation failed for {bin_path.name}: {e}")
            return False

    @staticmethod
    def validate_csv(csv_path: Path) -> bool:
        """Confirm a proxy CSV has the minimum expected shape."""
        try:
            import csv as csv_module
            with open(csv_path, 'r', encoding='utf-8') as f:
                reader = csv_module.reader(f)
                first_row = next(reader, [])
                if len(first_row) < 4:
                    logger.error("Proxy CSV validation failed - fewer than 4 columns")
                    return False
            return True
        except Exception as e:
            logger.error(f"Proxy CSV validation failed: {e}")
            return False

    def install(self, source: Path, target: Path, validator) -> bool:
        """Validate, then atomically move `source` into place at `target`."""
        if not validator(source):
            return False
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            # Write to a temp name on the same filesystem, then atomic rename so
            # the API never sees a half-written file during hot reload.
            tmp = target.with_suffix(target.suffix + '.tmp')
            shutil.copy2(source, tmp)
            os.replace(tmp, target)
            target.chmod(0o644)
            source.unlink(missing_ok=True)
            size = target.stat().st_size
            logger.info(f"Installed {target.name} ({size:,} bytes, md5 {self.calculate_md5(target)})")
            return True
        except Exception as e:
            logger.error(f"Failed to install {target.name}: {e}")
            return False

    # ------------------------------------------------------------------ #
    # Per-database update routines
    # ------------------------------------------------------------------ #
    def proxy_target_path(self) -> Path:
        if PROXY_DATABASE_PATH:
            return Path(PROXY_DATABASE_PATH)
        # Derive from the code, e.g. PX12LITECSVIPV6 -> IP2PROXY-LITE-PX12.IPV6.CSV
        name = self._lite_filename(PROXY_DATABASE_CODE)
        return self.db_dir / name[:-4]  # strip trailing ".ZIP"

    def update_geo(self) -> bool:
        logger.info("Updating geolocation database")
        zip_path = self.download(GEO_DATABASE_CODE, "geolocation database")
        if not zip_path:
            return False
        bin_path = self.extract_member(zip_path, '.BIN')
        if not bin_path:
            zip_path.unlink(missing_ok=True)
            return False
        target = Path(GEO_DATABASE_PATH)
        self.backup_file(target, "ip2location_geo")
        ok = self.install(bin_path, target, self.validate_bin)
        zip_path.unlink(missing_ok=True)
        return ok

    def update_asn(self) -> bool:
        if not ASN_DATABASE_CODE:
            logger.info("No IP2LOCATION_ASN_DATABASE_CODE set, skipping ASN update")
            return True
        logger.info("Updating ASN database")
        zip_path = self.download(ASN_DATABASE_CODE, "ASN database")
        if not zip_path:
            return False
        bin_path = self.extract_member(zip_path, '.BIN')
        if not bin_path:
            zip_path.unlink(missing_ok=True)
            return False
        target = Path(ASN_DATABASE_PATH)
        self.backup_file(target, "ip2location_asn")
        ok = self.install(bin_path, target, self.validate_bin)
        zip_path.unlink(missing_ok=True)
        return ok

    def update_proxy(self) -> bool:
        if not ENABLE_PROXY_DETECTION:
            logger.info("Proxy detection disabled, skipping proxy update")
            return True
        logger.info("Updating proxy database")
        zip_path = self.download(PROXY_DATABASE_CODE, "proxy database")
        if not zip_path:
            return False
        csv_path = self.extract_member(zip_path, '.CSV')
        if not csv_path:
            zip_path.unlink(missing_ok=True)
            return False
        target = self.proxy_target_path()
        self.backup_file(target, "ip2proxy")
        ok = self.install(csv_path, target, self.validate_csv)
        zip_path.unlink(missing_ok=True)
        return ok

    def record_status(self, results: dict) -> None:
        """Write a machine-readable status file covering all databases."""
        try:
            payload = {
                "timestamp": datetime.now().isoformat(),
                "databases": results,
            }
            (self.db_dir / "last_update.log").write_text(json.dumps(payload, indent=2))
        except Exception as e:
            logger.warning(f"Failed to write last_update.log: {e}")

    def perform_update(self) -> bool:
        """Update every enabled database. Returns True if geolocation succeeded."""
        logger.info("Starting database update")
        results = {}

        results['geolocation'] = {
            "code": GEO_DATABASE_CODE,
            "status": "success" if self.update_geo() else "failed",
        }
        results['asn'] = {
            "code": ASN_DATABASE_CODE,
            "status": ("success" if self.update_asn() else "failed") if ASN_DATABASE_CODE else "disabled",
        }
        results['proxy'] = {
            "code": PROXY_DATABASE_CODE,
            "status": ("success" if self.update_proxy() else "failed") if ENABLE_PROXY_DETECTION else "disabled",
        }

        self.record_status(results)

        geo_ok = results['geolocation']['status'] == "success"
        failed = [name for name, r in results.items() if r['status'] == "failed"]
        if failed:
            logger.warning(f"Update finished with failures: {', '.join(failed)}")
        else:
            logger.info("All enabled database updates completed successfully")
        return geo_ok

    # ------------------------------------------------------------------ #
    # Scheduling
    # ------------------------------------------------------------------ #
    def bootstrap_if_missing(self) -> None:
        """On startup, download anything that isn't on disk yet."""
        missing = []
        if not Path(GEO_DATABASE_PATH).exists():
            missing.append("geolocation")
        if ASN_DATABASE_CODE and not Path(ASN_DATABASE_PATH).exists():
            missing.append("asn")
        if ENABLE_PROXY_DETECTION and not self.proxy_target_path().exists():
            missing.append("proxy")

        if missing:
            logger.info(f"Missing on startup: {', '.join(missing)} - running initial download")
            self.perform_update()
        else:
            logger.info("All databases present on startup")

    def run_scheduler(self) -> None:
        """Run perform_update() on the UPDATE_SCHEDULE cron expression, forever."""
        schedule_expr = UPDATE_SCHEDULE.strip()
        if not croniter.is_valid(schedule_expr):
            logger.warning(f"Invalid UPDATE_SCHEDULE '{schedule_expr}', falling back to '0 2 * * *'")
            schedule_expr = '0 2 * * *'

        logger.info(f"Scheduler started (UPDATE_SCHEDULE='{schedule_expr}')")
        while True:
            now = datetime.now()
            next_run = croniter(schedule_expr, now).get_next(datetime)
            wait_seconds = (next_run - now).total_seconds()
            logger.info(f"Next update at {next_run.isoformat()} (in {int(wait_seconds)}s)")
            time.sleep(max(1, wait_seconds))
            try:
                self.perform_update()
            except Exception as e:
                logger.error(f"Scheduled update crashed (will retry next cycle): {e}")


def main():
    parser = argparse.ArgumentParser(description="IP2Location database updater")
    parser.add_argument('--loop', action='store_true',
                        help="Bootstrap missing databases, then run on UPDATE_SCHEDULE forever")
    args = parser.parse_args()

    logger.info("IP2Location database updater starting")
    updater = IP2LocationUpdater()

    if args.loop:
        updater.bootstrap_if_missing()
        updater.run_scheduler()  # never returns
    else:
        success = updater.perform_update()
        sys.exit(0 if success else 1)


if __name__ == '__main__':
    main()
