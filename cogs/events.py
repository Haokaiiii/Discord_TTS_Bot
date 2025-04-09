import discord
from discord.ext import commands
import logging
import traceback
import aiohttp

from utils.helpers import get_preferred_name, handle_command_error, send_to_command_channel
from utils.config import ALLOWED_COMMAND_CHANNEL_ID

class EventsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.command_channel = None # Cache command channel object
        logging.info("EventsCog initialized.")

    @commands.Cog.listener()
    async def on_ready(self):
        logging.info(f'Logged in as {self.bot.user.name} ({self.bot.user.id})')
        logging.info(f'discord.py version: {discord.__version__}')
        logging.info('Bot is ready and connected to guilds:')
        guild_count = 0
        for guild in self.bot.guilds:
            logging.info(f"- {guild.name} (ID: {guild.id})")
            guild_count += 1
            if not self.command_channel and ALLOWED_COMMAND_CHANNEL_ID != 0:
                 # Attempt to find and cache the command channel in the first available guild
                 # This assumes the command channel ID is the same across all guilds, 
                 # or we primarily care about one specific guild for general bot messages.
                 # If channel ID varies per guild, caching here is less useful.
                 try:
                    channel = guild.get_channel(ALLOWED_COMMAND_CHANNEL_ID) 
                    # Or use: channel = await self.bot.fetch_channel(ALLOWED_COMMAND_CHANNEL_ID)
                    if channel and isinstance(channel, discord.TextChannel):
                        self.command_channel = channel
                        logging.info(f"Cached command channel: {channel.name} ({channel.id}) in guild {guild.name}")
                    else:
                         logging.warning(f"Could not find or cache command channel {ALLOWED_COMMAND_CHANNEL_ID} in guild {guild.name}.")
                 except discord.Forbidden:
                     logging.error(f"Permission error trying to fetch command channel {ALLOWED_COMMAND_CHANNEL_ID} in guild {guild.name}.")
                 except discord.HTTPException as e:
                     logging.error(f"HTTP error fetching command channel {ALLOWED_COMMAND_CHANNEL_ID} in guild {guild.name}: {e}")

        logging.info(f"Bot is present in {guild_count} guilds.")
        await self.bot.change_presence(activity=discord.Game(name="语音统计与TTS"))
        
        # Attempt to send ready message to command channel if found
        if self.command_channel:
             try:
                 await self.command_channel.send(f"{self.bot.user.name} 已启动并准备就绪！")
             except Exception as e:
                 logging.error(f"Failed to send ready message to command channel {self.command_channel.id}: {e}")
        elif ALLOWED_COMMAND_CHANNEL_ID != 0:
             logging.warning("Cannot send ready message: Command channel not found or configured.")

    @commands.Cog.listener()
    async def on_command_error(self, ctx: commands.Context, error):
        """Handles errors for all commands globally."""
        await handle_command_error(ctx, error)

    @commands.Cog.listener()
    async def on_guild_join(self, guild):
        """Logs when the bot joins a new guild."""
        logging.info(f"Joined new guild: {guild.name} (ID: {guild.id})")
        # Optionally send a welcome message to the default channel or owner
        # Might need specific permissions

    @commands.Cog.listener()
    async def on_guild_remove(self, guild):
        """Logs when the bot is removed from a guild."""
        logging.warning(f"Removed from guild: {guild.name} (ID: {guild.id})")
        # Clean up any guild-specific data if necessary (handled in StatsCog)

    @commands.Cog.listener()
    async def on_disconnect(self):
        """Logs when the bot disconnects from Discord."""
        logging.warning("Bot disconnected from Discord.")
        # This might be temporary, reconnection is usually handled automatically

    @commands.Cog.listener()
    async def on_connect(self):
        """Logs when the bot successfully connects to Discord (after startup/reconnect)."""
        logging.info("Bot connected to Discord.")

    @commands.Cog.listener()
    async def on_resumed(self):
        """Logs when the bot resumes a session after a disconnection."""
        logging.info("Bot session resumed.")

    # Example command moved from old bot.py - belongs more in general/admin cog?
    # Keep it here for now as an example of an event-related command.
    @commands.command(name='check_nickname')
    @commands.check_any(commands.is_owner(), commands.has_permissions(manage_nicknames=True))
    async def check_nickname(self, ctx, member: discord.Member = None):
        """检查用户当前的昵称或显示名称。"""
        if member is None:
            member = ctx.author

        name_to_check = get_preferred_name(member)
        await ctx.send(f"用户 {member.mention} 的当前识别名称是: `{name_to_check}`")


async def setup(bot: commands.Bot):
    await bot.add_cog(EventsCog(bot)) 