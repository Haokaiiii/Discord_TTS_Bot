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
ALLOWED_COMMAND_CHANNEL_ID = int(os.getenv('ALLOWED_COMMAND_CHANNEL_ID', '0')) # Default to 0 if not set
EXCLUDED_VOICE_CHANNEL_IDS = set(map(int, os.getenv('EXCLUDED_VOICE_CHANNEL_IDS', '').split(',')))
if '' in EXCLUDED_VOICE_CHANNEL_IDS: # Handle empty string case
    EXCLUDED_VOICE_CHANNEL_IDS.remove('')

# TTS Settings
TTS_CACHE_DIR = os.getenv('TTS_CACHE_DIR', 'tts_cache')
FFMPEG_EXECUTABLE = os.getenv('FFMPEG_EXECUTABLE', 'ffmpeg')
DEBOUNCE_TIME = float(os.getenv('DEBOUNCE_TIME', '2.0'))

# Backup Settings
BACKUP_DIR = os.getenv('BACKUP_DIR', 'data_backup')

# Health Check Server
HEALTH_CHECK_PORT = int(os.getenv('HEALTH_CHECK_PORT', '8080'))
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
logging.info(f"Excluded Voice Channel IDs: {EXCLUDED_VOICE_CHANNEL_IDS}")
logging.info(f"TTS Cache Directory: {TTS_CACHE_DIR}")
logging.info(f"Backup Directory: {BACKUP_DIR}")
logging.info(f"Health Check Port: {HEALTH_CHECK_PORT}") 