
import os
import json
import logging
import threading

logger = logging.getLogger(__name__)

# Cache for shared credentials (application lifetime)
_credentials_cache = None
_cache_lock = threading.Lock()

def _load_shared_credentials_from_file(shared_credentials_file):
    """
    Internal function to load credentials from file.

    Args:
        shared_credentials_file (str): Path to the shared credentials JSON file.

    Returns:
        dict: Dictionary with 'api_key', 'access_token', and optionally 'openalgo_api_key' keys.

    Raises:
        FileNotFoundError: If the file doesn't exist.
        ValueError: If the file contains invalid/empty credentials.
        IOError: If the file cannot be read.
    """
    if not os.path.exists(shared_credentials_file):
        raise FileNotFoundError(f"Shared credentials file not found at: {shared_credentials_file}")

    with open(shared_credentials_file, 'r') as f:
        creds = json.load(f)

    api_key = creds.get('api_key')
    access_token = creds.get('access_token')
    openalgo_api_key = creds.get('openalgo_api_key')  # Load OpenAlgo API key for WebSocket auth

    if not api_key or not access_token:
        raise ValueError(f"Shared credentials file exists but contains empty keys. Path: {shared_credentials_file}")

    logger.info(f"Loaded shared credentials from: {shared_credentials_file}")

    # Return all credentials including openalgo_api_key if present
    result = {'api_key': api_key, 'access_token': access_token}
    if openalgo_api_key:
        result['openalgo_api_key'] = openalgo_api_key
    return result


def _get_or_load_shared_credentials():
    """
    Internal helper to get cached credentials or load them from file.
    This function is thread-safe and implements double-checked locking.
    
    Returns:
        dict or None: Cached credentials dictionary if in shared mode, None otherwise.
        
    Raises:
        FileNotFoundError: If the shared credentials file doesn't exist.
        ValueError: If the file contains invalid/empty credentials.
        IOError: If the file cannot be read.
    """
    global _credentials_cache
    
    shared_credentials_file = os.getenv('SHARED_CREDENTIALS_FILE')
    if not shared_credentials_file:
        return None
    
    # Check cache first (fast path, no lock needed for read)
    if _credentials_cache is not None:
        return _credentials_cache
    
    # Cache miss - acquire lock and load
    with _cache_lock:
        # Double-check cache after acquiring lock
        if _credentials_cache is not None:
            return _credentials_cache
        
        # Load from file and cache. _load_shared_credentials_from_file can raise exceptions.
        _credentials_cache = _load_shared_credentials_from_file(shared_credentials_file)
        return _credentials_cache


def get_shared_credentials(default_api_key, default_access_token):
    """
    Resolve credentials to use: either local defaults or shared credentials from a file.
    Credentials are cached in memory for application lifetime after first load.

    Args:
        default_api_key (str): The local API key provided by env/db.
        default_access_token (str): The local access token provided by env/db.

    Returns:
        tuple: (api_key, access_token)

    Raises:
        ValueError: If shared credentials are enabled (via env var) but invalid/missing.
        IOError: If the shared credentials file cannot be read.
    """
    try:
        credentials = _get_or_load_shared_credentials()
        if credentials:
            api_key = credentials['api_key']
            access_token = credentials['access_token']

            # Log comparison for debugging
            # if default_access_token == access_token:
            #     logger.info("TOKEN CHECK: SAME - Default access token matches shared access token")
            # else:
            #     logger.critical(
            #         "\n" + "!"*50 + "\n"
            #         "NOT SAME - Default access token DOES NOT match shared access token!\n"
            #         f"Default: {default_access_token}\n"
            #         f"Shared : {access_token}\n"
            #         + "!"*50
            #     )

            return api_key, access_token
    except Exception as e:
        logger.error(f"CRITICAL: Failed to load shared credentials: {e}")
        # Fail fast - do not fallback to local creds if shared mode was explicitly requested
        raise
    
    # Normal mode: use local credentials
    return default_api_key, default_access_token

def get_shared_auth_token(default_auth_token):
    """
    Resolve credentials to use: either local defaults or shared credentials from a file.
    Credentials are cached in memory for application lifetime after first load.

    Args:
        default_auth_token (str): The local auth token provided by env/db.

    Returns:
        str: auth_token

    Raises:
        ValueError: If shared credentials are enabled (via env var) but invalid/missing.
        IOError: If the shared credentials file cannot be read.
    """
    try:
        credentials = _get_or_load_shared_credentials()
        if credentials:
            api_key = credentials['api_key']
            access_token = credentials['access_token']
            shared_auth_token = f"{api_key}:{access_token}"

            # Check if they match. Note: default_auth_token usually comes as "api_key:access_token"
            # if default_auth_token == shared_auth_token:
            #     logger.info("TOKEN CHECK: SAME - Default auth token matches shared auth token")
            # else:
            #      logger.critical(
            #         "\n" + "!"*50 + "\n"
            #         "NOT SAME - Default auth token DOES NOT match shared auth token!\n"
            #         f"Default: {default_auth_token}\n"
            #         f"Shared : {shared_auth_token}\n"
            #         + "!"*50
            #     )

            return shared_auth_token
    except Exception as e:
        logger.error(f"CRITICAL: Failed to load shared credentials: {e}")
        # Fail fast - do not fallback to local creds if shared mode was explicitly requested
        raise
    
    # Normal mode: use local credentials
    return default_auth_token


def get_shared_openalgo_api_key():
    """
    Get the OpenAlgo API key from shared credentials file for WebSocket authentication.
    
    Returns:
        str or None: OpenAlgo API key if available in shared credentials, None otherwise.
    """
    try:
        credentials = _get_or_load_shared_credentials()
        if credentials:
            return credentials.get('openalgo_api_key')
    except Exception as e:
        logger.error(f"Failed to load shared credentials for OpenAlgo API key: {e}")
    
    return None
