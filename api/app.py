#!/usr/bin/env python3
"""
IP2Location API Service with Cloudflare Header Support
Binary database format only for optimal performance
"""

import os
import json
import time
import logging
import ipaddress
import csv
import hmac
import threading
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List
from pathlib import Path
from array import array

from flask import Flask, request, g, Response
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

# Import IP2Location for binary database support
try:
    import IP2Location
    BINARY_SUPPORT = True
except ImportError:
    BINARY_SUPPORT = False
    logging.error("IP2Location library not available. This service requires the IP2Location library.")
    exit(1)

# Configure logging
log_handlers = [logging.StreamHandler()]

# Try to add file handler, but handle permission errors gracefully
try:
    os.makedirs('/app/logs', exist_ok=True)
    log_handlers.append(logging.FileHandler('/app/logs/api.log'))
except (PermissionError, OSError) as e:
    print(f"Warning: Could not create log file, using console logging only: {e}")

logging.basicConfig(
    level=os.getenv('LOG_LEVEL', 'INFO'),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=log_handlers
)
logger = logging.getLogger(__name__)

# Initialize Flask app
app = Flask(__name__)
# Configurable CORS. Defaults to "*" (open) to preserve existing behavior; set
# CORS_ORIGINS to a comma-separated allowlist to lock it down.
_cors_origins = os.getenv('CORS_ORIGINS', '*').strip() or '*'
CORS(app, origins='*' if _cors_origins == '*' else [o.strip() for o in _cors_origins.split(',')])

# Configure JSON pretty printing
app.config['JSONIFY_PRETTYPRINT_REGULAR'] = True
app.json.sort_keys = False
app.json.ensure_ascii = False
app.json.indent = 2

# Configuration
app.config['IP2LOCATION_DATABASE_PATH'] = os.getenv('IP2LOCATION_DATABASE_PATH')
app.config['ASN_DATABASE_PATH'] = os.getenv('ASN_DATABASE_PATH')
app.config['PROXY_DATABASE_PATH'] = os.getenv('PROXY_DATABASE_PATH')
app.config['ENABLE_PROXY_DETECTION'] = os.getenv('ENABLE_PROXY_DETECTION', 'false').lower() == 'true'
app.config['ENABLE_NEARBY_PROXY_DETECTION'] = os.getenv('ENABLE_NEARBY_PROXY_DETECTION', 'true').lower() == 'true'
app.config['NEARBY_IP_SEARCH_DISTANCE'] = int(os.getenv('NEARBY_IP_SEARCH_DISTANCE', '1000'))
app.config['NEARBY_IP_MIN_MATCHES'] = int(os.getenv('NEARBY_IP_MIN_MATCHES', '3'))
app.config['API_KEY'] = os.getenv('API_KEY')
app.config['DISABLE_API_KEY_AUTH'] = os.getenv('DISABLE_API_KEY_AUTH', 'false').lower() == 'true'
app.config['ENABLE_CLOUDFLARE_HEADERS'] = os.getenv('ENABLE_CLOUDFLARE_HEADERS', 'true').lower() == 'true'
app.config['RATE_LIMIT_PER_MINUTE'] = int(os.getenv('RATE_LIMIT_PER_MINUTE', '100'))
app.config['REDIS_URL'] = os.getenv('REDIS_URL')
# How often (seconds) a request is allowed to stat the DB files for hot-reload.
# The databases change at most daily, so checking on every request wastes syscalls.
app.config['RELOAD_CHECK_INTERVAL'] = float(os.getenv('RELOAD_CHECK_INTERVAL_SECONDS', '5'))

# Initialize rate limiter
limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=[f"{app.config['RATE_LIMIT_PER_MINUTE']} per minute"],
    storage_uri=app.config['REDIS_URL']
)


def per_minute_limit() -> str:
    """Dynamic rate limit so RATE_LIMIT_PER_MINUTE is actually honored on routes
    that previously hardcoded their own value."""
    return f"{app.config['RATE_LIMIT_PER_MINUTE']} per minute"

# Global variables for database connections
binary_db = None
asn_db = None
binary_db_mtime = None
asn_db_mtime = None

# Global variables for proxy database
proxy_lookup_engine = None
proxy_db_mtime = None

# Hot-reload throttle + serialization. The lock prevents concurrent worker
# threads from rebuilding the same database at once (a stampede that would
# briefly hold two copies of the multi-GB proxy CSV in memory).
_reload_lock = threading.Lock()
_last_reload_check = 0.0

# Background hot-reload watcher. Reloading is done off the request path so the
# first query after a daily database update no longer blocks while the (large)
# proxy CSV is parsed — requests keep serving the old data until the swap is
# ready.
_watcher_started = False
_watcher_lock = threading.Lock()

# IP2Proxy CSV columns after (ip_from, ip_to), in file order. Used to map the
# stored row tuples back to named fields.
PROXY_FIELDS = (
    'proxy_type', 'country_code', 'country_name', 'region_name', 'city_name',
    'asn_name', 'domain', 'usage_type', 'asn', 'last_seen', 'threat',
    'residential', 'provider', 'fraud_score',
)

_MASK64 = (1 << 64) - 1  # for splitting 128-bit IPs into uint64 hi/lo halves

class ProxyLookupEngine:
    """High-performance CSV-based proxy lookup engine using in-memory IP ranges"""
    
    def __init__(self, csv_path: str):
        self.csv_path = csv_path
        # Compact columnar storage instead of a Python object per row:
        #   * IP bounds are 128-bit, split into uint64 hi/lo halves in `array`s
        #     (no per-row int objects).
        #   * Each of the 14 fields is dictionary-encoded — a small per-row index
        #     into that column's list of distinct values (each value stored once).
        # This holds the full PX12 IPv6 set in a fraction of the RAM the old
        # tuple/dict-per-row layout used, with the same binary-search lookup.
        self.s_hi = array('Q'); self.s_lo = array('Q')
        self.e_hi = array('Q'); self.e_lo = array('Q')
        self.col_idx = [array('I') for _ in PROXY_FIELDS]
        self.col_vals = [[] for _ in PROXY_FIELDS]
        self.headers = []
        self.n = 0
        self.total_ranges = 0
        self.load_csv()

    def _start(self, i: int) -> int:
        return (self.s_hi[i] << 64) | self.s_lo[i]

    def _end(self, i: int) -> int:
        return (self.e_hi[i] << 64) | self.e_lo[i]

    def _bisect_right(self, hi: int, lo: int) -> int:
        """bisect_right over the sorted (hi, lo) start keys."""
        s_hi, s_lo = self.s_hi, self.s_lo
        a, b = 0, self.n
        while a < b:
            m = (a + b) >> 1
            if s_hi[m] < hi or (s_hi[m] == hi and s_lo[m] <= lo):
                a = m + 1
            else:
                b = m
        return a

    def _bisect_left(self, hi: int, lo: int) -> int:
        """bisect_left over the sorted (hi, lo) start keys."""
        s_hi, s_lo = self.s_hi, self.s_lo
        a, b = 0, self.n
        while a < b:
            m = (a + b) >> 1
            if s_hi[m] < hi or (s_hi[m] == hi and s_lo[m] < lo):
                a = m + 1
            else:
                b = m
        return a

    def load_csv(self):
        """Load the proxy CSV into compact columnar arrays."""
        logger.info(f"Loading proxy database from {self.csv_path}")
        start_time = datetime.now(timezone.utc)

        s_hi = array('Q'); s_lo = array('Q'); e_hi = array('Q'); e_lo = array('Q')
        col_idx = [array('I') for _ in PROXY_FIELDS]   # uint32 during build
        col_dicts = [dict() for _ in PROXY_FIELDS]     # value -> index (dropped after load)
        col_vals = [[] for _ in PROXY_FIELDS]          # index -> distinct value
        nfields = len(PROXY_FIELDS)
        self.headers = []
        is_sorted = True
        prev = (-1, -1)

        try:
            with open(self.csv_path, 'r', encoding='utf-8') as f:
                reader = csv.reader(f)
                # IP2Proxy CSV files have no header row; detect width from data.
                for row in reader:
                    if len(row) < 4:  # need ip_from, ip_to, country_code, country_name
                        continue
                    if not self.headers:
                        self.headers = [f'field_{i}' for i in range(len(row))]
                    try:
                        ip_from = int(row[0].strip('"'))
                        ip_to = int(row[1].strip('"'))
                    except ValueError:
                        continue

                    hi, lo = ip_from >> 64, ip_from & _MASK64
                    if (hi, lo) < prev:
                        is_sorted = False
                    prev = (hi, lo)
                    s_hi.append(hi); s_lo.append(lo)
                    e_hi.append(ip_to >> 64); e_lo.append(ip_to & _MASK64)

                    # Columns 2.. map to PROXY_FIELDS in order; missing -> None.
                    for c in range(nfields):
                        col = c + 2
                        val = row[col].strip('"') if col < len(row) else None
                        d = col_dicts[c]
                        idx = d.get(val)
                        if idx is None:
                            idx = len(col_vals[c])
                            d[val] = idx
                            col_vals[c].append(val)
                        col_idx[c].append(idx)

            del col_dicts  # encoding done; free the value->index maps
            n = len(s_hi)

            # IP2Location CSVs ship sorted by ip_from; only reorder if that turns
            # out not to hold (keeps the common path single-pass and low-memory).
            if not is_sorted and n > 1:
                # Rare: IP2Location CSVs ship sorted. `perm` materializes n Python
                # ints (~28 B each, e.g. ~200 MB at 7M rows) for this one pass.
                logger.warning("Proxy CSV not sorted by ip_from; sorting in memory")
                perm = sorted(range(n), key=lambda i: (s_hi[i], s_lo[i]))
                s_hi = array('Q', (s_hi[i] for i in perm))
                s_lo = array('Q', (s_lo[i] for i in perm))
                e_hi = array('Q', (e_hi[i] for i in perm))
                e_lo = array('Q', (e_lo[i] for i in perm))
                col_idx = [array(col.typecode, (col[i] for i in perm)) for col in col_idx]
                del perm

            # Down-size each index column to the smallest int type that fits,
            # freeing the wide build array as we go to keep the peak down.
            sized = [None] * nfields
            for c in range(nfields):
                m = len(col_vals[c])
                typecode = 'B' if m <= 256 else ('H' if m <= 65536 else 'I')
                sized[c] = col_idx[c] if typecode == 'I' else array(typecode, col_idx[c])
                col_idx[c] = None

            self.s_hi, self.s_lo, self.e_hi, self.e_lo = s_hi, s_lo, e_hi, e_lo
            self.col_idx = sized
            self.col_vals = col_vals
            self.n = n
            self.total_ranges = n

            uniq = sum(len(v) for v in col_vals)
            load_time = (datetime.now(timezone.utc) - start_time).total_seconds()
            logger.info(f"Proxy database loaded: {n:,} ranges, "
                        f"{uniq:,} unique field values in {load_time:.2f}s")
        except Exception as e:
            logger.error(f"Failed to load proxy database: {e}")
            self.s_hi = array('Q'); self.s_lo = array('Q')
            self.e_hi = array('Q'); self.e_lo = array('Q')
            self.col_idx = [array('I') for _ in PROXY_FIELDS]
            self.col_vals = [[] for _ in PROXY_FIELDS]
            self.n = 0
            self.total_ranges = 0
    
    def _row_to_dict(self, i: int) -> Dict[str, Any]:
        """Decode stored row `i` back into a proxy-data dict (skipping absent fields)."""
        out = {}
        col_idx = self.col_idx
        col_vals = self.col_vals
        for c, field in enumerate(PROXY_FIELDS):
            val = col_vals[c][col_idx[c][i]]
            if val is not None:
                out[field] = val
        return out

    def lookup(self, ip: str) -> Optional[Dict[str, Any]]:
        """Lookup proxy information for an IP using binary search with nearby IP detection"""
        if not self.n:
            return None

        try:
            ip_obj = ipaddress.ip_address(ip)

            # For IPv4 addresses also check the IPv4-mapped IPv6 form, since the
            # combined dataset stores IPv4 ranges as ::ffff:x.
            ip_ints_to_check = [int(ip_obj)]
            if isinstance(ip_obj, ipaddress.IPv4Address):
                ip_ints_to_check.append(int(ipaddress.ip_address(f"::ffff:{ip}")))

            # Try each IP representation for an exact match first.
            for ip_int in ip_ints_to_check:
                hi, lo = ip_int >> 64, ip_int & _MASK64
                idx = self._bisect_right(hi, lo) - 1
                if 0 <= idx < self.n and self._start(idx) <= ip_int <= self._end(idx):
                    result = self._row_to_dict(idx)
                    result['detection_method'] = 'exact_match'
                    result['confidence'] = 'high'
                    return result

            # If no exact match and nearby detection is enabled, check nearby IPs.
            if app.config.get('ENABLE_NEARBY_PROXY_DETECTION', True):
                return self._check_nearby_proxies(ip_ints_to_check)

            return None

        except Exception as e:
            logger.error(f"Proxy lookup error for {ip}: {e}")
            return None
    
    def _check_nearby_proxies(self, ip_ints_to_check: List[int]) -> Optional[Dict[str, Any]]:
        """Check for proxy indicators in nearby IP ranges"""
        search_distance = app.config.get('NEARBY_IP_SEARCH_DISTANCE', 1000)
        min_matches = app.config.get('NEARBY_IP_MIN_MATCHES', 3)
        
        for ip_int in ip_ints_to_check:
            nearby_proxies = []
            nearby_ranges = []

            # Find the insertion point for binary search
            hi, lo = ip_int >> 64, ip_int & _MASK64
            idx = self._bisect_left(hi, lo)

            # Check ranges before the target IP
            for i in range(max(0, idx - 50), idx):
                ip_start = self._start(i)
                ip_end = self._end(i)

                # Calculate distance from our target IP
                distance = abs(ip_int - ip_end)
                if distance <= search_distance:
                    proxy_data = self._row_to_dict(i)
                    nearby_ranges.append({
                        'distance': distance,
                        'data': proxy_data,
                        'range_start': ip_start,
                        'range_end': ip_end
                    })
                    nearby_proxies.append(proxy_data)

            # Check ranges after the target IP
            for i in range(idx, min(self.n, idx + 50)):
                ip_start = self._start(i)
                ip_end = self._end(i)

                # Calculate distance from our target IP
                distance = abs(ip_start - ip_int)
                if distance <= search_distance:
                    proxy_data = self._row_to_dict(i)
                    nearby_ranges.append({
                        'distance': distance,
                        'data': proxy_data,
                        'range_start': ip_start,
                        'range_end': ip_end
                    })
                    nearby_proxies.append(proxy_data)

            # Analyze nearby proxy patterns
            if len(nearby_proxies) >= min_matches:
                return self._analyze_nearby_proxies(nearby_proxies, nearby_ranges, ip_int)

        return None
    
    def _analyze_nearby_proxies(self, nearby_proxies: List[Dict], nearby_ranges: List[Dict], target_ip: int) -> Dict[str, Any]:
        """Analyze nearby proxy data to determine if target IP is likely a proxy"""
        # Count proxy types and providers
        proxy_types = {}
        providers = {}
        countries = {}
        
        closest_range = None
        min_distance = float('inf')
        
        for range_info in nearby_ranges:
            proxy_data = range_info['data']
            distance = range_info['distance']
            
            # Track the closest range
            if distance < min_distance:
                min_distance = distance
                closest_range = range_info
            
            # Count occurrences
            proxy_type = proxy_data.get('proxy_type', 'Unknown')
            provider = proxy_data.get('provider', proxy_data.get('asn_name', 'Unknown'))
            country = proxy_data.get('country_code', 'Unknown')
            
            proxy_types[proxy_type] = proxy_types.get(proxy_type, 0) + 1
            providers[provider] = providers.get(provider, 0) + 1
            countries[country] = countries.get(country, 0) + 1
        
        # Find most common attributes
        most_common_type = max(proxy_types.items(), key=lambda x: x[1])[0]
        most_common_provider = max(providers.items(), key=lambda x: x[1])[0]
        most_common_country = max(countries.items(), key=lambda x: x[1])[0]
        
        # Determine confidence based on consistency and proximity
        total_nearby = len(nearby_proxies)
        type_consistency = proxy_types[most_common_type] / total_nearby
        provider_consistency = providers[most_common_provider] / total_nearby
        
        # Calculate confidence score
        if min_distance <= 100 and type_consistency >= 0.8 and provider_consistency >= 0.8:
            confidence = 'high'
        elif min_distance <= 500 and type_consistency >= 0.6 and provider_consistency >= 0.6:
            confidence = 'medium'
        else:
            confidence = 'low'
        
        # Create response based on closest range but with aggregated data
        result = {
            'proxy_type': most_common_type,
            'country_code': most_common_country,
            'country_name': closest_range['data'].get('country_name', ''),
            'provider': most_common_provider,
            'detection_method': 'nearby_analysis',
            'confidence': confidence,
            'nearby_analysis': {
                'total_nearby_ranges': total_nearby,
                'closest_distance': min_distance,
                'proxy_types': proxy_types,
                'providers': providers,
                'countries': countries,
                'type_consistency': round(type_consistency, 2),
                'provider_consistency': round(provider_consistency, 2)
            }
        }
        
        # Add optional fields from closest range
        if closest_range and closest_range['data']:
            closest_data = closest_range['data']
            for field in ['region_name', 'city_name', 'asn_name', 'domain', 'usage_type', 'asn', 'last_seen', 'threat', 'residential', 'fraud_score']:
                if closest_data.get(field):
                    result[field] = closest_data[field]
        
        return result
    
    def get_stats(self) -> Dict[str, Any]:
        """Get proxy database statistics"""
        return {
            'total_ranges': self.total_ranges,
            'database_path': self.csv_path,
            'headers': self.headers,
            'database_level': self.detect_database_level()
        }
    
    def detect_database_level(self) -> str:
        """Auto-detect proxy database level from headers"""
        field_count = len(self.headers)
        level_map = {
            4: 'PX1',   # country
            5: 'PX2',   # + proxy_type
            7: 'PX3',   # + region, city
            8: 'PX4',   # + asn_name
            9: 'PX5',   # + domain
            10: 'PX6',  # + usage_type
            11: 'PX7',  # + asn
            12: 'PX8',  # + last_seen
            13: 'PX9',  # + threat
            14: 'PX10', # + residential
            15: 'PX11', # + provider
            16: 'PX12', # + fraud_score
        }
        return level_map.get(field_count, f'Unknown({field_count} fields)')

def check_and_reload_database(force: bool = False):
    """Check if database files have been updated and reload if necessary.

    Throttled to at most one filesystem check per RELOAD_CHECK_INTERVAL seconds
    and serialized by a lock, so a single request no longer triggers it three
    times and concurrent requests can't rebuild the same DB simultaneously.
    Pass force=True (e.g. the manual /reload endpoint) to bypass the throttle.
    """
    global binary_db, asn_db, binary_db_mtime, asn_db_mtime, proxy_lookup_engine, proxy_db_mtime
    global _last_reload_check

    if not BINARY_SUPPORT:
        return False

    interval = app.config['RELOAD_CHECK_INTERVAL']
    now = time.monotonic()
    # Fast path: skip without taking the lock or hitting the filesystem.
    if not force and (now - _last_reload_check) < interval:
        return False

    with _reload_lock:
        # Re-check inside the lock: another thread may have just refreshed.
        now = time.monotonic()
        if not force and (now - _last_reload_check) < interval:
            return False
        _last_reload_check = now

        return _reload_databases()


def _reload_databases():
    """Stat each configured database file and reload any that changed on disk.
    Caller must hold _reload_lock."""
    global binary_db, asn_db, binary_db_mtime, asn_db_mtime, proxy_lookup_engine, proxy_db_mtime

    reloaded = False

    # Check main geolocation database
    binary_path = Path(app.config['IP2LOCATION_DATABASE_PATH'])
    if binary_path.exists():
        current_mtime = binary_path.stat().st_mtime
        if binary_db_mtime is None or current_mtime != binary_db_mtime:
            try:
                binary_db = IP2Location.IP2Location(str(binary_path))
                binary_db_mtime = current_mtime
                logger.info(f"Geolocation database reloaded: {binary_path}")
                reloaded = True
            except Exception as e:
                logger.error(f"Failed to reload geolocation database: {e}")
    
    # Check ASN database (optional)
    if app.config['ASN_DATABASE_PATH']:
        asn_path = Path(app.config['ASN_DATABASE_PATH'])
        if asn_path.exists():
            current_mtime = asn_path.stat().st_mtime
            if asn_db_mtime is None or current_mtime != asn_db_mtime:
                try:
                    asn_db = IP2Location.IP2Location(str(asn_path))
                    asn_db_mtime = current_mtime
                    logger.info(f"ASN database reloaded: {asn_path}")
                    reloaded = True
                except Exception as e:
                    logger.error(f"Failed to reload ASN database: {e}")
    
    # Check proxy database (optional)
    if app.config['ENABLE_PROXY_DETECTION'] and app.config['PROXY_DATABASE_PATH']:
        proxy_path = Path(app.config['PROXY_DATABASE_PATH'])
        if proxy_path.exists():
            current_mtime = proxy_path.stat().st_mtime
            if proxy_db_mtime is None or current_mtime != proxy_db_mtime:
                try:
                    proxy_lookup_engine = ProxyLookupEngine(str(proxy_path))
                    proxy_db_mtime = current_mtime
                    logger.info(f"Proxy database reloaded: {proxy_path}")
                    reloaded = True
                except Exception as e:
                    logger.error(f"Failed to reload proxy database: {e}")

    return reloaded


def _db_watcher_loop():
    """Periodically reload changed databases in the background.

    The actual parse/build (e.g. the large proxy CSV) happens on this thread, so
    request threads never block on it. The global engine/db references are only
    swapped once the new object is fully built, so in-flight lookups keep using
    the old data until then.
    """
    interval = max(1.0, app.config['RELOAD_CHECK_INTERVAL'])
    while True:
        time.sleep(interval)
        try:
            with _reload_lock:
                _reload_databases()
        except Exception as e:
            logger.error(f"Background database reload check failed: {e}")


def start_db_watcher():
    """Start the background reload watcher once per process (idempotent).

    Must be called *after* fork under gunicorn (threads don't survive the fork
    from a preloaded master), so it's invoked from the gunicorn post_fork hook,
    the dev-server __main__ block, and lazily on the first request.
    """
    global _watcher_started
    if _watcher_started:
        return
    with _watcher_lock:
        if _watcher_started:
            return
        _watcher_started = True
        threading.Thread(target=_db_watcher_loop, name="db-watcher", daemon=True).start()
        logger.info(f"Database hot-reload watcher started (interval={app.config['RELOAD_CHECK_INTERVAL']}s)")


def init_database():
    """Initialize binary database connections"""
    global binary_db, asn_db, binary_db_mtime, asn_db_mtime, proxy_lookup_engine, proxy_db_mtime
    
    if not BINARY_SUPPORT:
        logger.error("IP2Location library not available")
        return False
    
    success = True
    
    # Initialize main geolocation database
    binary_path = Path(app.config['IP2LOCATION_DATABASE_PATH'])
    if binary_path.exists():
        try:
            binary_db = IP2Location.IP2Location(str(binary_path))
            binary_db_mtime = binary_path.stat().st_mtime
            logger.info(f"Geolocation database initialized: {binary_path}")
        except Exception as e:
            logger.error(f"Failed to initialize geolocation database: {e}")
            success = False
    else:
        logger.warning(f"Geolocation database file not found: {binary_path}")
        success = False
    
    # Initialize ASN database (optional)
    if app.config['ASN_DATABASE_PATH']:
        asn_path = Path(app.config['ASN_DATABASE_PATH'])
        if asn_path.exists():
            try:
                asn_db = IP2Location.IP2Location(str(asn_path))
                asn_db_mtime = asn_path.stat().st_mtime
                logger.info(f"ASN database initialized: {asn_path}")
            except Exception as e:
                logger.error(f"Failed to initialize ASN database: {e}")
                # ASN is optional, don't fail the whole initialization
        else:
            logger.warning(f"ASN database file not found: {asn_path}")
    
    # Initialize proxy database (optional)
    if app.config['ENABLE_PROXY_DETECTION'] and app.config['PROXY_DATABASE_PATH']:
        proxy_path = Path(app.config['PROXY_DATABASE_PATH'])
        if proxy_path.exists():
            try:
                proxy_lookup_engine = ProxyLookupEngine(str(proxy_path))
                proxy_db_mtime = proxy_path.stat().st_mtime
                logger.info(f"Proxy database initialized: {proxy_path}")
            except Exception as e:
                logger.error(f"Failed to initialize proxy database: {e}")
                # Proxy is optional, don't fail the whole initialization
        else:
            logger.warning(f"Proxy database file not found: {proxy_path}")
    
    return success

# Initialize database after app configuration
if not init_database():
    logger.error("Failed to initialize the database. The application will not start.")
    # Don't exit here as this will prevent gunicorn from starting
    # Instead, the lookup functions will return appropriate errors

def pretty_json_response(data, status_code=200):
    """Create a pretty-printed JSON response"""
    response = Response(
        json.dumps(data, indent=2, ensure_ascii=False),
        status=status_code,
        mimetype='application/json'
    )
    return response

def get_real_ip() -> str:
    """
    Extract the real client IP address from request headers.
    Supports Cloudflare headers: CF-Connecting-IP, True-Client-IP, X-Forwarded-For
    """
    if not app.config['ENABLE_CLOUDFLARE_HEADERS']:
        return request.remote_addr or '127.0.0.1'
    
    # Priority order for Cloudflare headers
    headers_to_check = [
        'CF-Connecting-IP',      # Primary Cloudflare header
        'True-Client-IP',        # Enterprise Cloudflare header
        'X-Forwarded-For',       # Standard proxy header
        'X-Real-IP',             # Nginx real IP header
    ]
    
    for header in headers_to_check:
        ip = request.headers.get(header)
        if ip:
            # Handle X-Forwarded-For which can contain multiple IPs
            if header == 'X-Forwarded-For' and ',' in ip:
                ip = ip.split(',')[0].strip()
            
            # Validate IP address
            try:
                ipaddress.ip_address(ip)
                logger.debug(f"Real IP extracted from {header}: {ip}")
                return ip
            except ValueError:
                logger.warning(f"Invalid IP address in {header}: {ip}")
                continue
    
    # Fallback to remote_addr
    return request.remote_addr or '127.0.0.1'

def validate_api_key() -> bool:
    """Validate the API key from request headers or query parameters.

    Fails closed: if authentication is enabled but no real key is configured,
    every request is denied. (Previously an unset API_KEY made the comparison
    ``None == None`` succeed, authenticating everyone.)
    """
    if app.config['DISABLE_API_KEY_AUTH']:
        return True

    configured_key = app.config.get('API_KEY')
    if not configured_key or configured_key == 'your_secure_api_key':
        logger.error(
            "API key authentication is enabled but API_KEY is unset or still the "
            "default placeholder; denying request. Set a real API_KEY or "
            "DISABLE_API_KEY_AUTH=true."
        )
        return False

    provided_key = request.headers.get('X-API-Key') or request.args.get('api_key')
    if not provided_key:
        return False

    # Constant-time comparison to avoid leaking the key via timing.
    return hmac.compare_digest(provided_key, configured_key)

def lookup_asn_info(ip: str) -> Dict[str, Any]:
    """Lookup ASN information using ASN database"""
    # Reload is handled once per request by the caller (lookup_ip_location).
    if not asn_db:
        return {
            "asn": None,
            "asn_name": None
        }
    
    try:
        result = asn_db.get_all(ip)
        
        def safe_string(value):
            if isinstance(value, str) and ("unavailable" in value.lower() or "upgrade" in value.lower() or value in ['-', '']):
                return None
            return value if value != '-' else None
        
        def safe_int(value):
            if isinstance(value, str) and ("unavailable" in value.lower() or "upgrade" in value.lower() or value in ['-', '']):
                return None
            try:
                return int(value) if value != 0 and value != '-' else None
            except (ValueError, TypeError):
                return None
        
        return {
            "asn": safe_int(result.asn),
            "asn_name": safe_string(result.as_name)
        }
        
    except Exception as e:
        logger.error(f"ASN database lookup error: {e}")
        return {
            "asn": None,
            "asn_name": None
        }

def lookup_proxy_info(ip: str) -> Dict[str, Any]:
    """Lookup proxy information using CSV database with nearby IP detection"""
    # Reload is handled once per request by the caller (lookup_ip_location).
    if not app.config['ENABLE_PROXY_DETECTION'] or not proxy_lookup_engine:
        return {
            "is_proxy": False,
            "proxy_type": None,
            "proxy_country": None
        }
    
    try:
        result = proxy_lookup_engine.lookup(ip)
        
        if result:
            # Format proxy response based on available data
            proxy_response = {
                "is_proxy": True,
                "proxy_type": result.get('proxy_type'),
                "proxy_country": result.get('country_code'),
                "proxy_country_name": result.get('country_name'),
                "detection_method": result.get('detection_method', 'exact_match'),
                "confidence": result.get('confidence', 'high')
            }
            
            # Add optional fields if available
            if result.get('region_name'):
                proxy_response["proxy_region"] = result.get('region_name')
            if result.get('city_name'):
                proxy_response["proxy_city"] = result.get('city_name')
            if result.get('asn_name'):
                proxy_response["proxy_asn_name"] = result.get('asn_name')
            if result.get('domain'):
                proxy_response["proxy_domain"] = result.get('domain')
            if result.get('usage_type'):
                proxy_response["proxy_usage_type"] = result.get('usage_type')
            if result.get('asn'):
                proxy_response["proxy_asn"] = result.get('asn')
            if result.get('last_seen'):
                proxy_response["proxy_last_seen"] = result.get('last_seen')
            if result.get('threat'):
                proxy_response["proxy_threat"] = result.get('threat')
            if result.get('residential'):
                proxy_response["proxy_residential"] = result.get('residential')
            if result.get('provider'):
                proxy_response["proxy_provider"] = result.get('provider')
            if result.get('fraud_score'):
                proxy_response["proxy_fraud_score"] = result.get('fraud_score')
            
            # Add nearby analysis data if available
            if result.get('nearby_analysis'):
                proxy_response["nearby_analysis"] = result.get('nearby_analysis')
            
            return proxy_response
        else:
            return {
                "is_proxy": False,
                "proxy_type": None,
                "proxy_country": None,
                "detection_method": "not_found",
                "confidence": None
            }
        
    except Exception as e:
        logger.error(f"Proxy database lookup error: {e}")
        return {
            "is_proxy": False,
            "proxy_type": None,
            "proxy_country": None,
            "detection_method": "error",
            "confidence": None
        }

def lookup_ip_location(ip: str) -> Dict[str, Any]:
    """Lookup IP location using binary database"""
    # Database reloads are handled by the background watcher thread, so this
    # request path never blocks on parsing an updated database.
    if not binary_db:
        raise Exception("Binary database not initialized")
    
    try:
        result = binary_db.get_all(ip)
        
        if result.country_short == '-' or result.country_short == 'INVALID IP ADDRESS':
            # Still try to get ASN and proxy info even if geolocation failed
            asn_info = lookup_asn_info(ip)
            proxy_info = lookup_proxy_info(ip)
            
            return {
                "ip": ip,
                "error": "IP address not found in database",
                "country_code": None,
                "country_name": None,
                "region_name": None,
                "city_name": None,
                "latitude": None,
                "longitude": None,
                "zip_code": None,
                "time_zone": None,
                "asn": asn_info["asn"],
                "asn_name": asn_info["asn_name"],
                "proxy": proxy_info
            }
        
        # Helper function to safely convert numeric fields
        def safe_float(value):
            if isinstance(value, str) and ("unavailable" in value.lower() or "upgrade" in value.lower()):
                return None
            # Note: do NOT treat 0 as "missing" — 0.0 is a valid coordinate
            # (equator / prime meridian) and was previously dropped to null.
            try:
                return float(value) if value != '-' else None
            except (ValueError, TypeError):
                return None
        
        def safe_string(value):
            if isinstance(value, str) and ("unavailable" in value.lower() or "upgrade" in value.lower()):
                return None
            return value if value != '-' else None

        # Get ASN information
        asn_info = lookup_asn_info(ip)
        
        # Get proxy information
        proxy_info = lookup_proxy_info(ip)
        
        return {
            "ip": ip,
            "country_code": safe_string(result.country_short),
            "country_name": safe_string(result.country_long),
            "region_name": safe_string(result.region),
            "city_name": safe_string(result.city),
            "latitude": safe_float(result.latitude),
            "longitude": safe_float(result.longitude),
            "zip_code": safe_string(result.zipcode),
            "time_zone": safe_string(result.timezone),
            "asn": asn_info["asn"],
            "asn_name": asn_info["asn_name"],
            "proxy": proxy_info
        }
        
    except Exception as e:
        logger.error(f"Binary database lookup error: {e}")
        raise Exception("Database lookup failed")

@app.before_request
def before_request():
    """Log incoming requests with real IP information"""
    # Ensure the background reload watcher is running in this worker process
    # (cheap no-op after the first request; covers servers that don't call the
    # gunicorn post_fork hook).
    start_db_watcher()

    real_ip = get_real_ip()
    g.real_ip = real_ip
    g.start_time = datetime.now(timezone.utc)
    
    # Skip logging for health endpoint
    if request.path == '/health':
        return
    
    # Log request details
    logger.info(f"Request from {real_ip}: {request.method} {request.path}")
    
    # Log Cloudflare headers for debugging
    if app.config['ENABLE_CLOUDFLARE_HEADERS']:
        cf_headers = {
            'CF-Connecting-IP': request.headers.get('CF-Connecting-IP'),
            'True-Client-IP': request.headers.get('True-Client-IP'),
            'X-Forwarded-For': request.headers.get('X-Forwarded-For'),
            'CF-Ray': request.headers.get('CF-Ray'),
            'CF-IPCountry': request.headers.get('CF-IPCountry')
        }
        cf_headers = {k: v for k, v in cf_headers.items() if v}
        if cf_headers:
            logger.debug(f"Cloudflare headers: {cf_headers}")

@app.after_request
def after_request(response):
    """Log response information"""
    # Skip logging for health endpoint
    if request.path == '/health':
        return response
    
    duration = (datetime.now(timezone.utc) - g.start_time).total_seconds()
    logger.info(f"Response to {g.real_ip}: {response.status_code} in {duration:.3f}s")
    return response

@app.route('/')
@limiter.limit(per_minute_limit)
def root():
    """Simplified IP lookup endpoint"""
    # validate_api_key() already short-circuits when auth is disabled.
    if not validate_api_key():
        return pretty_json_response({"error": "Invalid or missing API key"}, 401)
    
    # Get IP address from query parameter or use client IP
    ip = request.args.get('ip')
    if not ip:
        ip = get_real_ip()
    
    try:
        # Validate IP address format
        ipaddress.ip_address(ip)
        
        # Perform lookup
        result = lookup_ip_location(ip)
        
        # Add metadata
        result.update({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "client_ip": get_real_ip(),
            "queried_ip": ip
        })
        
        return pretty_json_response(result)
        
    except ValueError as e:
        logger.warning(f"Invalid IP address provided: {ip}")
        return pretty_json_response({"error": f"Invalid IP address: {ip}"}, 400)
    except Exception as e:
        logger.error(f"Error processing lookup request: {e}")
        return pretty_json_response({"error": "Internal server error"}, 500)

@app.route('/health')
def health():
    """Health check endpoint"""
    geo_db_status = "unavailable"
    asn_db_status = "unavailable"
    proxy_db_status = "unavailable"
    
    # Test geolocation database
    try:
        if binary_db:
            test_result = binary_db.get_all("8.8.8.8")
            geo_db_status = "healthy"
        else:
            geo_db_status = "not_initialized"
    except Exception as e:
        geo_db_status = f"error: {str(e)}"
    
    # Test ASN database
    try:
        if asn_db:
            test_result = asn_db.get_all("8.8.8.8")
            asn_db_status = "healthy"
        else:
            asn_db_status = "not_initialized"
    except Exception as e:
        asn_db_status = f"error: {str(e)}"
    
    # Test proxy database
    try:
        if app.config['ENABLE_PROXY_DETECTION'] and proxy_lookup_engine:
            test_result = proxy_lookup_engine.lookup("8.8.8.8")
            proxy_db_status = "healthy"
            proxy_stats = proxy_lookup_engine.get_stats()
        elif not app.config['ENABLE_PROXY_DETECTION']:
            proxy_db_status = "disabled"
            proxy_stats = None
        else:
            proxy_db_status = "not_initialized"
            proxy_stats = None
    except Exception as e:
        proxy_db_status = f"error: {str(e)}"
        proxy_stats = None
    
    overall_status = "healthy" if geo_db_status == "healthy" else "degraded"
    
    health_response = {
        "status": overall_status,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "databases": {
            "geolocation": {
                "status": geo_db_status,
                "type": "binary",
                "path": app.config['IP2LOCATION_DATABASE_PATH']
            },
            "asn": {
                "status": asn_db_status,
                "type": "binary",
                "path": app.config['ASN_DATABASE_PATH']
            },
            "proxy": {
                "status": proxy_db_status,
                "type": "csv",
                "path": app.config['PROXY_DATABASE_PATH'],
                "enabled": app.config['ENABLE_PROXY_DETECTION']
            }
        },
        "cloudflare_headers": app.config['ENABLE_CLOUDFLARE_HEADERS'],
        "version": "2.1.0"
    }
    
    # Add proxy statistics if available
    if proxy_stats:
        health_response["databases"]["proxy"]["stats"] = proxy_stats
    
    return pretty_json_response(health_response)

@app.route('/api/v1/lookup')
@limiter.limit(per_minute_limit)
def lookup_ip():
    """
    Lookup IP geolocation
    Query parameters:
    - ip: IP address to lookup (optional, defaults to client IP)
    - api_key: API key for authentication
    """
    # Validate API key
    if not validate_api_key():
        return pretty_json_response({"error": "Invalid or missing API key"}, 401)
    
    # Get IP address from query parameter or use client IP
    ip = request.args.get('ip')
    if not ip:
        ip = get_real_ip()
    
    try:
        # Validate IP address format
        ipaddress.ip_address(ip)
        
        # Perform lookup
        result = lookup_ip_location(ip)
        
        # Add metadata
        result.update({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "client_ip": get_real_ip(),
            "queried_ip": ip
        })
        
        return pretty_json_response(result)
        
    except ValueError as e:
        logger.warning(f"Invalid IP address provided: {ip}")
        return pretty_json_response({"error": f"Invalid IP address: {ip}"}, 400)
    except Exception as e:
        logger.error(f"Error processing lookup request: {e}")
        return pretty_json_response({"error": "Internal server error"}, 500)

@app.route('/api/v1/lookup/batch', methods=['POST'])
@limiter.limit("10 per minute")
def lookup_batch():
    """
    Batch IP lookup endpoint
    Request body should contain JSON with 'ips' array
    """
    if not validate_api_key():
        return pretty_json_response({"error": "Invalid or missing API key"}, 401)
    
    try:
        data = request.get_json()
        if not data or 'ips' not in data:
            return pretty_json_response({"error": "Missing 'ips' array in request body"}, 400)
        
        ips = data['ips']
        if not isinstance(ips, list) or len(ips) > 100:
            return pretty_json_response({"error": "Invalid 'ips' array (max 100 IPs)"}, 400)
        
        results = []
        for ip in ips:
            try:
                ipaddress.ip_address(ip)
                result = lookup_ip_location(ip)
                results.append(result)
            except ValueError:
                results.append({
                    "ip": ip,
                    "error": "Invalid IP address format"
                })
            except Exception as e:
                results.append({
                    "ip": ip,
                    "error": "Lookup failed"
                })
        
        return pretty_json_response({
            "results": results,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "client_ip": get_real_ip(),
            "total_queries": len(ips)
        })
        
    except Exception as e:
        logger.error(f"Error processing batch lookup: {e}")
        return pretty_json_response({"error": "Internal server error"}, 500)


@app.route('/api/v1/reload')
@limiter.limit("5 per minute")
def reload_database():
    """Manually reload database files"""
    if not validate_api_key():
        return pretty_json_response({"error": "Invalid or missing API key"}, 401)
    
    try:
        # Manual trigger: bypass the per-request throttle.
        reloaded = check_and_reload_database(force=True)

        return pretty_json_response({
            "status": "success",
            "reloaded": reloaded,
            "message": "Database reload check completed" if reloaded else "No database changes detected",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "databases": {
                "geolocation": {
                    "loaded": binary_db is not None,
                    "mtime": binary_db_mtime
                },
                "asn": {
                    "loaded": asn_db is not None,
                    "mtime": asn_db_mtime
                },
                "proxy": {
                    "loaded": proxy_lookup_engine is not None,
                    "mtime": proxy_db_mtime,
                    "enabled": app.config['ENABLE_PROXY_DETECTION']
                }
            }
        })
        
    except Exception as e:
        logger.error(f"Database reload failed: {e}")
        return pretty_json_response({"error": "Database reload failed"}, 500)

@app.route('/api/v1/info')
def api_info():
    """API information endpoint"""
    return pretty_json_response({
        "service": "IP2Location LITE API",
        "version": "2.1.0",
        "database_type": "binary",
        "endpoints": {
            "/": "Simplified IP lookup",
            "/health": "Health check",
            "/api/v1/lookup": "Single IP lookup",
            "/api/v1/lookup/batch": "Batch IP lookup",
            "/api/v1/reload": "Manual database reload",
            "/api/v1/info": "API information"
        },
        "features": {
            "cloudflare_headers": app.config['ENABLE_CLOUDFLARE_HEADERS'],
            "rate_limiting": True,
            "api_key_authentication": not app.config['DISABLE_API_KEY_AUTH'],
            "binary_database": True,
            "hot_reload": True,
            "microsecond_lookup_times": True,
            "asn_lookup": asn_db is not None,
            "geolocation_lookup": binary_db is not None,
            "proxy_detection": app.config['ENABLE_PROXY_DETECTION'] and proxy_lookup_engine is not None,
            "nearby_proxy_detection": app.config.get('ENABLE_NEARBY_PROXY_DETECTION', True),
            "nearby_ip_search_distance": app.config.get('NEARBY_IP_SEARCH_DISTANCE', 1000),
            "nearby_ip_min_matches": app.config.get('NEARBY_IP_MIN_MATCHES', 3)
        },
        "attribution": "This service uses IP2Location LITE data available from https://www.ip2location.com",
        "client_ip": get_real_ip(),
        "timestamp": datetime.now(timezone.utc).isoformat()
    })

@app.errorhandler(429)
def ratelimit_handler(e):
    """Rate limit exceeded handler"""
    return pretty_json_response({
        "error": "Rate limit exceeded",
        "message": "Too many requests. Please try again later.",
        "client_ip": get_real_ip()
    }, 429)

@app.errorhandler(500)
def internal_error(e):
    """Internal server error handler"""
    logger.error(f"Internal server error: {e}")
    return pretty_json_response({
        "error": "Internal server error",
        "message": "An unexpected error occurred"
    }, 500)

if __name__ == '__main__':
    logger.info("Starting IP2Location API service...")
    logger.info(f"Cloudflare headers enabled: {app.config['ENABLE_CLOUDFLARE_HEADERS']}")
    logger.info(f"Rate limit: {app.config['RATE_LIMIT_PER_MINUTE']} requests per minute")
    logger.info(f"API key authentication enabled: {not app.config['DISABLE_API_KEY_AUTH']}")

    # Background DB hot-reload watcher (gunicorn starts it via post_fork instead).
    start_db_watcher()

    app.run(
        host='0.0.0.0',
        port=5000,
        debug=False,
        threaded=True
    )
