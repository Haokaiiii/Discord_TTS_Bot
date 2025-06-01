import logging
import asyncio
import signal
import sys
from typing import Optional
import discord
from discord.ext import commands

from utils.logging_config import setup_logging
from utils.config import DISCORD_TOKEN, COMMAND_PREFIX
from utils.health import start_health_server
from utils.database import DatabaseManager
from utils.helpers import clear_channel_cache

# Setup logging first
setup_logging()

# Configure intents
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
intents.members = True
intents.guilds = True
intents.presences = False  # Not needed, save bandwidth

class DiscordTTSBot(commands.Bot):
    """Main bot class with improved error handling and lifecycle management."""
    
    def __init__(self):
        super().__init__(
            command_prefix=commands.when_mentioned_or(COMMAND_PREFIX),
            intents=intents,
            help_command=None,  # We'll implement a custom help command
            case_insensitive=True,
            strip_after_prefix=True,
            activity=discord.Game(name="语音统计与TTS"),
            status=discord.Status.online
        )
        self.db_manager: Optional[DatabaseManager] = None
        self.health_runner = None
        self._shutdown_event = asyncio.Event()
        self._extensions_loaded = False
        
    async def setup_hook(self):
        """Initialize bot components during setup."""
        logging.info("Bot setup hook started")
        
        # Initialize database
        try:
            self.db_manager = DatabaseManager()
            logging.info("Database manager initialized")
        except Exception as e:
            logging.critical(f"Failed to initialize database: {e}")
            raise
        
        # Load extensions
        await self.load_extensions()
        
        # Start health check server
        try:
            await start_health_server(self)
            logging.info("Health check server started")
        except Exception as e:
            logging.error(f"Failed to start health check server: {e}")
            # Non-critical, continue
        
        logging.info("Bot setup completed")
    
    async def load_extensions(self):
        """Load all cog extensions with error handling."""
        extensions = [
            'cogs.events',
            'cogs.stats', 
            'cogs.tts'
        ]
        
        for extension in extensions:
            try:
                await self.load_extension(extension)
                logging.info(f"Loaded extension: {extension}")
            except Exception as e:
                logging.error(f"Failed to load extension {extension}: {e}", exc_info=True)
                # Continue loading other extensions
        
        self._extensions_loaded = True
        
    async def on_ready(self):
        """Called when the bot is fully ready."""
        logging.info(f"Bot ready: {self.user} (ID: {self.user.id})")
        logging.info(f"Connected to {len(self.guilds)} guilds")
        
    async def on_guild_join(self, guild: discord.Guild):
        """Handle bot joining a new guild."""
        logging.info(f"Joined guild: {guild.name} (ID: {guild.id})")
        clear_channel_cache(guild.id)  # Clear any cached channel data
        
    async def on_guild_remove(self, guild: discord.Guild):
        """Handle bot removal from a guild."""
        logging.info(f"Removed from guild: {guild.name} (ID: {guild.id})")
        clear_channel_cache(guild.id)
        
    async def on_error(self, event: str, *args, **kwargs):
        """Handle errors in event handlers."""
        logging.error(f"Error in event {event}", exc_info=True)
        
    async def close(self):
        """Gracefully shutdown the bot."""
        logging.info("Bot shutdown initiated")
        
        # Signal shutdown to prevent new operations
        self._shutdown_event.set()
        
        # Close health check server
        if hasattr(self, 'health_runner') and self.health_runner:
            try:
                await self.health_runner.cleanup()
                logging.info("Health check server stopped")
            except Exception as e:
                logging.error(f"Error stopping health server: {e}")
        
        # Save any pending data
        if self.db_manager:
            try:
                # Get stats cog and save data
                stats_cog = self.get_cog('StatsCog')
                if stats_cog:
                    logging.info("Saving final statistics...")
                    await stats_cog.save_stats()
                    
                    # Cancel scheduled tasks
                    if hasattr(stats_cog, 'save_stats') and stats_cog.save_stats.is_running():
                        stats_cog.save_stats.cancel()
                    
                    if hasattr(stats_cog, 'scheduler'):
                        stats_cog.scheduler.cancel()
                        
            except Exception as e:
                logging.error(f"Error saving final stats: {e}")
        
        # Disconnect voice clients
        try:
            tts_cog = self.get_cog('TTSCog')
            if tts_cog:
                logging.info("Disconnecting voice clients...")
                # The cog_unload method will handle cleanup
        except Exception as e:
            logging.error(f"Error during TTS cleanup: {e}")
        
        # Unload extensions
        if self._extensions_loaded:
            for extension in list(self.extensions):
                try:
                    await self.unload_extension(extension)
                    logging.info(f"Unloaded extension: {extension}")
                except Exception as e:
                    logging.error(f"Error unloading {extension}: {e}")
        
        # Close database connections
        if self.db_manager:
            try:
                self.db_manager.close()
                await self.db_manager.aclose()
                logging.info("Database connections closed")
            except Exception as e:
                logging.error(f"Error closing database: {e}")
        
        # Clear caches
        clear_channel_cache()
        
        # Call parent close
        await super().close()
        logging.info("Bot shutdown complete")

async def main():
    """Main entry point with proper lifecycle management."""
    bot = DiscordTTSBot()
    
    # Setup signal handlers for graceful shutdown
    def signal_handler(sig, frame):
        logging.info(f"Received signal {sig}")
        asyncio.create_task(bot.close())
    
    # Register signal handlers
    if sys.platform != "win32":
        # Unix-like systems
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, signal_handler)
    else:
        # Windows - only SIGINT is reliable
        signal.signal(signal.SIGINT, signal_handler)
    
    try:
        # Start the bot
        async with bot:
            await bot.start(DISCORD_TOKEN)
            
    except discord.LoginFailure:
        logging.critical("Invalid Discord token")
        return 1
        
    except discord.PrivilegedIntentsRequired:
        logging.critical("Bot requires privileged intents that are not enabled")
        return 1
        
    except Exception as e:
        logging.critical(f"Fatal error: {e}", exc_info=True)
        return 1
        
    return 0

if __name__ == "__main__":
    try:
        # Run the bot
        exit_code = asyncio.run(main())
        sys.exit(exit_code)
        
    except KeyboardInterrupt:
        logging.info("Received keyboard interrupt")
        sys.exit(0)
        
    except Exception as e:
        logging.critical(f"Unhandled exception: {e}", exc_info=True)
        sys.exit(1) 