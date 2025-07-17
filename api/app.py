#!/usr/bin/env python3
"""
IP2Location API Service with Cloudflare Header Support
Binary database format only for optimal performance
"""

import os
import json
import logging
import ipaddress
from datetime import datetime
from typing import Optional, Dict, Any
from pathlib import Path

from flask import Flask, request, jsonify, g
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
CORS(app)

# Configuration
app.config['IP2LOCATION_DATABASE_PATH'] = os.getenv('IP2LOCATION_DATABASE_PATH')
app.config['API_KEY'] = os.getenv('API_KEY')
app.config['DISABLE_API_KEY_AUTH'] = os.getenv('DISABLE_API_KEY_AUTH', 'false').lower() == 'true'
app.config['ENABLE_CLOUDFLARE_HEADERS'] = os.getenv('ENABLE_CLOUDFLARE_HEADERS', 'true').lower() == 'true'
app.config['RATE_LIMIT_PER_MINUTE'] = int(os.getenv('RATE_LIMIT_PER_MINUTE', '100'))
app.config['REDIS_URL'] = os.getenv('REDIS_URL')

# Initialize rate limiter
limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=[f"{app.config['RATE_LIMIT_PER_MINUTE']} per minute"],
    storage_uri=app.config['REDIS_URL']
)

# Global variable for binary database connection
binary_db = None

def init_database():
    """Initialize binary database connection"""
    global binary_db
    
    if not BINARY_SUPPORT:
        logger.error("IP2Location library not available")
        return False
    
    binary_path = Path(app.config['IP2LOCATION_DATABASE_PATH'])
    if binary_path.exists():
        try:
            binary_db = IP2Location.IP2Location(str(binary_path))
            logger.info(f"Binary database initialized: {binary_path}")
            return True
        except Exception as e:
            logger.error(f"Failed to initialize binary database: {e}")
            return False
    else:
        logger.warning(f"Binary database file not found: {binary_path}")
        return False

# Initialize database after app configuration
if not init_database():
    logger.error("Failed to initialize the database. The application will not start.")
    # Don't exit here as this will prevent gunicorn from starting
    # Instead, the lookup functions will return appropriate errors

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
    """Validate API key from request headers or query parameters"""
    if app.config['DISABLE_API_KEY_AUTH']:
        logger.debug("API key authentication is disabled")
        return True

    api_key = request.headers.get('X-API-Key') or request.args.get('api_key')
    return api_key == app.config['API_KEY']

def lookup_ip_location(ip: str) -> Dict[str, Any]:
    """Lookup IP location using binary database"""
    if not binary_db:
        raise Exception("Binary database not initialized")
    
    try:
        result = binary_db.get_all(ip)
        
        if result.country_short == '-' or result.country_short == 'INVALID IP ADDRESS':
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
                "time_zone": None
            }
        
        # Helper function to safely convert numeric fields
        def safe_float(value):
            if isinstance(value, str) and ("unavailable" in value.lower() or "upgrade" in value.lower()):
                return None
            try:
                return float(value) if value != 0 and value != '-' else None
            except (ValueError, TypeError):
                return None
        
        def safe_string(value):
            if isinstance(value, str) and ("unavailable" in value.lower() or "upgrade" in value.lower()):
                return None
            return value if value != '-' else None

        return {
            "ip": ip,
            "country_code": safe_string(result.country_short),
            "country_name": safe_string(result.country_long),
            "region_name": safe_string(result.region),
            "city_name": safe_string(result.city),
            "latitude": safe_float(result.latitude),
            "longitude": safe_float(result.longitude),
            "zip_code": safe_string(result.zipcode),
            "time_zone": safe_string(result.timezone)
        }
        
    except Exception as e:
        logger.error(f"Binary database lookup error: {e}")
        raise Exception("Database lookup failed")

@app.before_request
def before_request():
    """Log incoming requests with real IP information"""
    real_ip = get_real_ip()
    g.real_ip = real_ip
    g.start_time = datetime.utcnow()
    
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
    duration = (datetime.utcnow() - g.start_time).total_seconds()
    logger.info(f"Response to {g.real_ip}: {response.status_code} in {duration:.3f}s")
    return response

@app.route('/')
@limiter.limit("100 per minute")
def root():
    """Simplified IP lookup endpoint"""
    # Validate API key if authentication is enabled
    if not app.config['DISABLE_API_KEY_AUTH'] and not validate_api_key():
        return jsonify({"error": "Invalid or missing API key"}), 401
    
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
            "timestamp": datetime.utcnow().isoformat(),
            "client_ip": get_real_ip(),
            "queried_ip": ip
        })
        
        return jsonify(result)
        
    except ValueError as e:
        logger.warning(f"Invalid IP address provided: {ip}")
        return jsonify({"error": f"Invalid IP address: {ip}"}), 400
    except Exception as e:
        logger.error(f"Error processing lookup request: {e}")
        return jsonify({"error": "Internal server error"}), 500

@app.route('/health')
def health():
    """Health check endpoint"""
    db_status = "unavailable"
    
    try:
        if binary_db:
            # Test binary database with a simple lookup
            test_result = binary_db.get_all("8.8.8.8")
            db_status = "healthy"
        else:
            db_status = "not_initialized"
    except Exception as e:
        db_status = f"error: {str(e)}"
    
    return jsonify({
        "status": "healthy" if db_status == "healthy" else "degraded",
        "timestamp": datetime.utcnow().isoformat(),
        "database": {
            "status": db_status,
            "type": "binary",
            "path": app.config['IP2LOCATION_DATABASE_PATH']
        },
        "cloudflare_headers": app.config['ENABLE_CLOUDFLARE_HEADERS'],
        "version": "2.0.0"
    })

@app.route('/api/v1/lookup')
@limiter.limit("50 per minute")
def lookup_ip():
    """
    Lookup IP geolocation
    Query parameters:
    - ip: IP address to lookup (optional, defaults to client IP)
    - api_key: API key for authentication
    """
    # Validate API key
    if not validate_api_key():
        return jsonify({"error": "Invalid or missing API key"}), 401
    
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
            "timestamp": datetime.utcnow().isoformat(),
            "client_ip": get_real_ip(),
            "queried_ip": ip
        })
        
        return jsonify(result)
        
    except ValueError as e:
        logger.warning(f"Invalid IP address provided: {ip}")
        return jsonify({"error": f"Invalid IP address: {ip}"}), 400
    except Exception as e:
        logger.error(f"Error processing lookup request: {e}")
        return jsonify({"error": "Internal server error"}), 500

@app.route('/api/v1/lookup/batch', methods=['POST'])
@limiter.limit("10 per minute")
def lookup_batch():
    """
    Batch IP lookup endpoint
    Request body should contain JSON with 'ips' array
    """
    if not validate_api_key():
        return jsonify({"error": "Invalid or missing API key"}), 401
    
    try:
        data = request.get_json()
        if not data or 'ips' not in data:
            return jsonify({"error": "Missing 'ips' array in request body"}), 400
        
        ips = data['ips']
        if not isinstance(ips, list) or len(ips) > 100:
            return jsonify({"error": "Invalid 'ips' array (max 100 IPs)"}), 400
        
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
        
        return jsonify({
            "results": results,
            "timestamp": datetime.utcnow().isoformat(),
            "client_ip": get_real_ip(),
            "total_queries": len(ips)
        })
        
    except Exception as e:
        logger.error(f"Error processing batch lookup: {e}")
        return jsonify({"error": "Internal server error"}), 500

@app.route('/api/v1/info')
def api_info():
    """API information endpoint"""
    return jsonify({
        "service": "IP2Location LITE API",
        "version": "2.0.0",
        "database_type": "binary",
        "endpoints": {
            "/": "Simplified IP lookup",
            "/health": "Health check",
            "/api/v1/lookup": "Single IP lookup",
            "/api/v1/lookup/batch": "Batch IP lookup",
            "/api/v1/info": "API information"
        },
        "features": {
            "cloudflare_headers": app.config['ENABLE_CLOUDFLARE_HEADERS'],
            "rate_limiting": True,
            "api_key_authentication": not app.config['DISABLE_API_KEY_AUTH'],
            "binary_database": True,
            "microsecond_lookup_times": True
        },
        "attribution": "This service uses IP2Location LITE data available from https://www.ip2location.com",
        "client_ip": get_real_ip(),
        "timestamp": datetime.utcnow().isoformat()
    })

@app.errorhandler(429)
def ratelimit_handler(e):
    """Rate limit exceeded handler"""
    return jsonify({
        "error": "Rate limit exceeded",
        "message": "Too many requests. Please try again later.",
        "client_ip": get_real_ip()
    }), 429

@app.errorhandler(500)
def internal_error(e):
    """Internal server error handler"""
    logger.error(f"Internal server error: {e}")
    return jsonify({
        "error": "Internal server error",
        "message": "An unexpected error occurred"
    }), 500

if __name__ == '__main__':
    logger.info("Starting IP2Location API service...")
    logger.info(f"Cloudflare headers enabled: {app.config['ENABLE_CLOUDFLARE_HEADERS']}")
    logger.info(f"Rate limit: {app.config['RATE_LIMIT_PER_MINUTE']} requests per minute")
    logger.info(f"API key authentication enabled: {not app.config['DISABLE_API_KEY_AUTH']}")
    
    app.run(
        host='0.0.0.0',
        port=5000,
        debug=False,
        threaded=True
    ) 