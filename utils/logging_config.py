"""Logging configuration utilities for the Discord TTS bot.

Provides a single entry point to configure structured, Docker-friendly logging.
"""
import logging
import sys

def setup_logging():
    """Configure root logging for containerized runtime.

    Configures logging to write structured messages to stdout, forces
    reconfiguration if a basicConfig already exists, and enables line-buffered
    stdout to reduce log latency in Docker.

    Returns
    -------
    None
    """
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s',
        stream=sys.stdout,  # Ensure logs go to stdout for Docker
        force=True  # Force reconfiguration if already configured
    )
    
    # Ensure immediate flushing for Docker
    for handler in logging.root.handlers:
        handler.flush = lambda: sys.stdout.flush()
    
    # Set unbuffered output
    sys.stdout.reconfigure(line_buffering=True)
    
    logging.info("Logging configured for Docker environment")