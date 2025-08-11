"""Configuration utilities for environment variables.

Centralizes reading and validating environment variables for the bot. Public
helpers use NumPy-style docstrings and perform basic validation, clamping, and
fallbacks while emitting informative logs.
"""
import os
import logging
from typing import Set, Optional
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

def get_env_str(key: str, default: Optional[str] = None, required: bool = True) -> Optional[str]:
    """Get a string environment variable with validation.

    Parameters
    ----------
    key : str
        Environment variable name.
    default : str or None, optional
        Default value if the variable is missing.
    required : bool, default True
        If True and the variable is missing, the process exits with code 1.

    Returns
    -------
    str or None
        Resolved value, default, or None when not required.
    """
    value = os.getenv(key, default)
    if required and not value:
        logging.error(f"Missing required environment variable: {key}")
        exit(1)
    return value

def get_env_int(key: str, default: int = 0, min_val: Optional[int] = None, max_val: Optional[int] = None) -> int:
    """Get an integer environment variable with validation and clamping.

    Parameters
    ----------
    key : str
        Environment variable name.
    default : int, default 0
        Default integer to use when parsing fails.
    min_val : int or None, optional
        Minimum allowed value; values below are clamped and a warning is logged.
    max_val : int or None, optional
        Maximum allowed value; values above are clamped and a warning is logged.

    Returns
    -------
    int
        Validated and possibly clamped integer value.
    """
    str_val = os.getenv(key, str(default))
    try:
        int_val = int(str_val)
        if min_val is not None and int_val < min_val:
            logging.warning(f"{key}={int_val} is below minimum {min_val}. Using minimum.")
            return min_val
        if max_val is not None and int_val > max_val:
            logging.warning(f"{key}={int_val} is above maximum {max_val}. Using maximum.")
            return max_val
        return int_val
    except ValueError:
        logging.error(f"Invalid integer value '{str_val}' for {key}. Using default {default}.")
        return default

def get_env_float(key: str, default: float = 0.0, min_val: Optional[float] = None) -> float:
    """Get a float environment variable with validation and optional floor.

    Parameters
    ----------
    key : str
        Environment variable name.
    default : float, default 0.0
        Default float to use when parsing fails.
    min_val : float or None, optional
        Minimum allowed value; values below are clamped and a warning is logged.

    Returns
    -------
    float
        Validated and possibly clamped float value.
    """
    str_val = os.getenv(key, str(default))
    try:
        float_val = float(str_val)
        if min_val is not None and float_val < min_val:
            logging.warning(f"{key}={float_val} is below minimum {min_val}. Using minimum.")
            return min_val
        return float_val
    except ValueError:
        logging.error(f"Invalid float value '{str_val}' for {key}. Using default {default}.")
        return default

def get_env_set_int(key: str, default: str = '') -> Set[int]:
    """Get a set of integers from a comma-separated environment variable.

    Parameters
    ----------
    key : str
        Environment variable name.
    default : str, default ''
        Fallback comma-separated value when missing.

    Returns
    -------
    set of int
        Set of parsed integers; invalid items are ignored with a warning.
    """
    str_val = os.getenv(key, default)
    result = set()
    
    if not str_val.strip():
        return result
        
    for item in str_val.split(','):
        item = item.strip()
        if item.isdigit():
            result.add(int(item))
        elif item:
            logging.warning(f"Invalid non-numeric value '{item}' in {key}. Ignoring.")
    
    return result

# Bot Token - Required
DISCORD_TOKEN = get_env_str('DISCORD_TOKEN', required=True)

# MongoDB URI - Required
MONGODB_URI = get_env_str('MONGODB_URI', required=True)

# Bot Settings
COMMAND_PREFIX = get_env_str('COMMAND_PREFIX', default='!', required=False)

# Allowed Command Channel ID (0 means any channel)
ALLOWED_COMMAND_CHANNEL_ID = get_env_int('ALLOWED_COMMAND_CHANNEL_ID', default=0, min_val=0)

# Excluded Voice Channel IDs
EXCLUDED_VOICE_CHANNEL_IDS = get_env_set_int('EXCLUDED_VOICE_CHANNEL_IDS')

# TTS Settings
TTS_CACHE_DIR = get_env_str('TTS_CACHE_DIR', default='tts_cache', required=False)
FFMPEG_EXECUTABLE = get_env_str('FFMPEG_EXECUTABLE', default='ffmpeg', required=False)

# Debounce Time (minimum 0.1 seconds)
DEBOUNCE_TIME = get_env_float('DEBOUNCE_TIME', default=2.0, min_val=0.1)

# Backup Settings
BACKUP_DIR = get_env_str('BACKUP_DIR', default='data_backup', required=False)
MAX_BACKUP_FILES = get_env_int('MAX_BACKUP_FILES', default=10, min_val=1, max_val=100)

# Health Check Server
HEALTH_CHECK_PORT = get_env_int('HEALTH_CHECK_PORT', default=8080, min_val=1024, max_val=65535)
HEALTH_CHECK_HOST = get_env_str('HEALTH_CHECK_HOST', default='0.0.0.0', required=False)

# Font Configuration
FONT_PATH = get_env_str('FONT_PATH', default='/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc', required=False)

# Performance Settings
MAX_PLOT_SIZE = get_env_int('MAX_PLOT_SIZE', default=40, min_val=10, max_val=100)
TTS_QUEUE_SIZE = get_env_int('TTS_QUEUE_SIZE', default=10, min_val=5, max_val=50)
TTS_TIMEOUT = get_env_float('TTS_TIMEOUT', default=45.0, min_val=10.0)

# Create necessary directories
for directory in [TTS_CACHE_DIR, BACKUP_DIR]:
    try:
        os.makedirs(directory, exist_ok=True)
        logging.info(f"Ensured directory exists: {directory}")
    except OSError as e:
        logging.error(f"Failed to create directory {directory}: {e}")
        exit(1)

# Log loaded configuration
logging.info("Configuration loaded successfully.")
logging.info(f"Command Prefix: {COMMAND_PREFIX}")
logging.info(f"Allowed Command Channel ID: {ALLOWED_COMMAND_CHANNEL_ID if ALLOWED_COMMAND_CHANNEL_ID != 0 else 'Any channel'}")
logging.info(f"Excluded Voice Channel IDs: {EXCLUDED_VOICE_CHANNEL_IDS if EXCLUDED_VOICE_CHANNEL_IDS else 'None'}")
logging.info(f"TTS Cache Directory: {TTS_CACHE_DIR}")
logging.info(f"Debounce Time: {DEBOUNCE_TIME}s")
logging.info(f"Backup Directory: {BACKUP_DIR} (Max Files: {MAX_BACKUP_FILES})")
logging.info(f"Health Check: {HEALTH_CHECK_HOST}:{HEALTH_CHECK_PORT}")
logging.info(f"Font Path: {FONT_PATH}")
logging.info(f"Performance: Max Plot Size={MAX_PLOT_SIZE}, TTS Queue={TTS_QUEUE_SIZE}, TTS Timeout={TTS_TIMEOUT}s") 