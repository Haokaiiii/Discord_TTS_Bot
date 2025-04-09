import discord
from discord.ext import commands
import logging
import asyncio
import os
import sys
import signal # For handling signals

# Initializers and Setup
from utils.logging_config import setup_logging
setup_logging() # Setup logging first

from utils.config import DISCORD_TOKEN, COMMAND_PREFIX
from utils.database import DatabaseManager # Import to ensure connection logic is available if needed early
from utils.health import start_health_server # Import health check
import backoff # For retry logic
import aiohttp # For health check server and potential bot connection errors

# Define Intents
intents = discord.Intents.default()
intents.voice_states = True
intents.guilds = True
intents.messages = True
intents.members = True # Required for on_member_update, on_voice_state_update, member lookups
intents.message_content = True # Required for commands

# Define Bot
# Consider using AutoShardedBot if you expect to scale beyond 2000 guilds
bot = commands.Bot(command_prefix=COMMAND_PREFIX, intents=intents)
bot.health_runner = None # Placeholder for health server runner
bot.report_scheduler = None # Placeholder for APScheduler instance from StatsCog

# List of Cogs to load
INITIAL_EXTENSIONS = [
    'cogs.events',
    'cogs.tts',
    'cogs.stats',
    # Add other cogs like 'cogs.admin' here if created
]

async def load_extensions():
    """Loads the initial cogs for the bot."""
    for extension in INITIAL_EXTENSIONS:
        try:
            await bot.load_extension(extension)
            logging.info(f"Successfully loaded extension: {extension}")
        except commands.ExtensionNotFound:
            logging.error(f"Extension not found: {extension}")
        except commands.ExtensionAlreadyLoaded:
            logging.warning(f"Extension already loaded: {extension}")
        except commands.NoEntryPointError:
            logging.error(f"Extension {extension} has no setup function.")
        except commands.ExtensionFailed as e:
            logging.error(f"Extension {extension} failed to load: {e.__cause__}", exc_info=True)
        except Exception as e:
            logging.error(f"An unexpected error occurred while loading extension {extension}: {e}", exc_info=True)

@backoff.on_exception(
    backoff.expo, 
    (aiohttp.ClientConnectorError, discord.errors.ConnectionClosed, asyncio.TimeoutError),
    max_tries=10, # Increase max retries
    max_time=600, # Increase max time
    on_backoff=lambda details: logging.warning(f"Connection error, backing off {details['wait']:.1f}s after {details['tries']} tries..."),
    on_giveup=lambda details: logging.critical("Bot connection failed after multiple retries. Giving up.")
)
async def run_bot():
    """Starts the bot, handling potential connection issues with backoff."""
    async with bot: # Use async context manager for proper cleanup
        await load_extensions() 
        await start_health_server(bot) # Start health check server
        logging.info("Starting bot...")
        await bot.start(DISCORD_TOKEN)

async def shutdown(signal, loop):
    """Graceful shutdown procedure."""
    logging.info(f"Received exit signal {signal.name}...")
    logging.info("Shutting down tasks and extensions...")

    # Cancel background tasks (adjust based on actual tasks used)
    tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    [task.cancel() for task in tasks]
    logging.info(f"Cancelling {len(tasks)} outstanding tasks...")
    await asyncio.gather(*tasks, return_exceptions=True)
    logging.info("Tasks cancelled.")

    # Stop scheduler if running
    if bot.report_scheduler and bot.report_scheduler.running:
        logging.info("Shutting down report scheduler...")
        try:
             bot.report_scheduler.shutdown(wait=False)
             logging.info("Scheduler shut down.")
        except Exception as e:
             logging.error(f"Error shutting down scheduler: {e}")

    # Close the bot connection
    if bot.is_ready():
        logging.info("Closing bot connection...")
        await bot.close()
        logging.info("Bot connection closed.")
    else:
         logging.warning("Bot was not ready, skipping close().")

    # Shutdown health check server
    if bot.health_runner:
        logging.info("Shutting down health check server...")
        await bot.health_runner.cleanup()
        logging.info("Health check server shut down.")

    # Close MongoDB connection (assuming DatabaseManager holds the client)
    # This requires accessing the client instance, maybe pass db_manager to main?
    # Or have DatabaseManager provide a class method for cleanup?
    # For simplicity, assuming MongoClient handles its own cleanup to some extent.
    logging.info("Database client cleanup implicitly handled by MongoClient driver.")

    loop.stop()
    logging.info("Shutdown complete.")


if __name__ == "__main__":
    loop = asyncio.get_event_loop()

    # Register signal handlers for graceful shutdown
    signals = (signal.SIGHUP, signal.SIGTERM, signal.SIGINT)
    for s in signals:
        try:
            loop.add_signal_handler(
                s, lambda s=s: asyncio.create_task(shutdown(s, loop))
            )
            logging.info(f"Registered signal handler for {s.name}")
        except NotImplementedError:
             logging.warning(f"Signal handler for {s.name} not implemented on this platform.")
             # Windows might not support all signals

    try:
        loop.run_until_complete(run_bot())
    except KeyboardInterrupt:
         logging.info("KeyboardInterrupt received, initiating shutdown...")
         # This might trigger the SIGINT handler anyway, but good to have
         loop.run_until_complete(shutdown(signal.SIGINT, loop))
    except Exception as e:
         logging.critical(f"Critical error during bot execution: {e}", exc_info=True)
         # Attempt graceful shutdown even on unexpected top-level error
         loop.run_until_complete(shutdown(signal.SIGTERM, loop))
    finally:
        if not loop.is_closed():
            logging.info("Closing event loop.")
            loop.close() 