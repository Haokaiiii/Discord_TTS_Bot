import os
import logging
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Bot Token
DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
if not DISCORD_TOKEN:
    logging.error("Missing DISCORD_TOKEN environment variable.")
    exit(1)

# MongoDB URI
MONGODB_URI = os.getenv('MONGODB_URI')
if not MONGODB_URI:
    logging.error("Missing MONGODB_URI environment variable.")
    exit(1)

# Bot Settings
COMMAND_PREFIX = os.getenv('COMMAND_PREFIX', '!')

# --- ALLOWED_COMMAND_CHANNEL_ID ---
allowed_channel_id_str = os.getenv('ALLOWED_COMMAND_CHANNEL_ID', '0')
try:
    ALLOWED_COMMAND_CHANNEL_ID = int(allowed_channel_id_str)
except ValueError:
    logging.error(f"Invalid value '{allowed_channel_id_str}' for ALLOWED_COMMAND_CHANNEL_ID. Must be an integer. Using default 0.")
    ALLOWED_COMMAND_CHANNEL_ID = 0

# --- EXCLUDED_VOICE_CHANNEL_IDS ---
excluded_ids_str = os.getenv('EXCLUDED_VOICE_CHANNEL_IDS', '')
excluded_ids_list = excluded_ids_str.split(',')
EXCLUDED_VOICE_CHANNEL_IDS = set()
for item in excluded_ids_list:
    item = item.strip() # Remove leading/trailing whitespace
    if item.isdigit(): # Check if it's actually a number
        EXCLUDED_VOICE_CHANNEL_IDS.add(int(item))
    elif item: # Log if it's not empty but also not a digit
        logging.warning(f"Invalid non-numeric value '{item}' found in EXCLUDED_VOICE_CHANNEL_IDS environment variable. Ignoring.")
# The old check `if '' in EXCLUDED_VOICE_CHANNEL_IDS:` is no longer needed.

# TTS Settings
TTS_CACHE_DIR = os.getenv('TTS_CACHE_DIR', 'tts_cache')
FFMPEG_EXECUTABLE = os.getenv('FFMPEG_EXECUTABLE', 'ffmpeg')

# --- DEBOUNCE_TIME ---
debounce_time_str = os.getenv('DEBOUNCE_TIME', '2.0')
try:
    DEBOUNCE_TIME = float(debounce_time_str)
except ValueError:
    logging.error(f"Invalid value '{debounce_time_str}' for DEBOUNCE_TIME. Must be a float. Using default 2.0.")
    DEBOUNCE_TIME = 2.0


# Backup Settings
BACKUP_DIR = os.getenv('BACKUP_DIR', 'data_backup')

# Health Check Server
# --- HEALTH_CHECK_PORT ---
health_port_str = os.getenv('HEALTH_CHECK_PORT', '8080')
try:
    HEALTH_CHECK_PORT = int(health_port_str)
except ValueError:
    logging.error(f"Invalid value '{health_port_str}' for HEALTH_CHECK_PORT. Must be an integer. Using default 8080.")
    HEALTH_CHECK_PORT = 8080

HEALTH_CHECK_HOST = os.getenv('HEALTH_CHECK_HOST', '0.0.0.0')

# Paths (Consider making these configurable if needed)
FONT_PATH = '/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc' # Specific to Docker image

# Create necessary directories
os.makedirs(TTS_CACHE_DIR, exist_ok=True)
os.makedirs(BACKUP_DIR, exist_ok=True)

# Log loaded config
logging.info("Configuration loaded.")
logging.info(f"Command Prefix: {COMMAND_PREFIX}")
logging.info(f"Allowed Command Channel ID: {ALLOWED_COMMAND_CHANNEL_ID if ALLOWED_COMMAND_CHANNEL_ID != 0 else 'None'}")
logging.info(f"Excluded Voice Channel IDs: {EXCLUDED_VOICE_CHANNEL_IDS if EXCLUDED_VOICE_CHANNEL_IDS else 'None'}")
logging.info(f"TTS Cache Directory: {TTS_CACHE_DIR}")
logging.info(f"Debounce Time: {DEBOUNCE_TIME}")
logging.info(f"Backup Directory: {BACKUP_DIR}")
logging.info(f"Health Check Port: {HEALTH_CHECK_PORT}")
logging.info(f"FFmpeg Executable: {FFMPEG_EXECUTABLE}")
logging.info(f"Font Path: {FONT_PATH}") 