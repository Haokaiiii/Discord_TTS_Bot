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
        import platform
        
        # Try to import psutil, but don't fail if it's not available
        try:
            import psutil
            memory_info = f"Memory: {psutil.virtual_memory().percent}%"
            cpu_info = f"CPU: {psutil.cpu_percent()}%"
        except ImportError:
            logging.warning("psutil not available, skipping system resource info")
            memory_info = "Memory: N/A"
            cpu_info = "CPU: N/A"
        
        logging.info(f'🤖 {self.bot.user.name} ({self.bot.user.id}) 已启动')
        logging.info(f'📦 discord.py 版本: {discord.__version__}')
        logging.info(f'🐍 Python 版本: {platform.python_version()}')
        logging.info(f'💻 系统: {platform.system()} {platform.release()}')
        logging.info(f'🧠 {memory_info}')
        logging.info(f'💾 {cpu_info}')
        
        logging.info('🏠 机器人已连接到以下服务器:')
        guild_count = 0
        total_members = 0
        for guild in self.bot.guilds:
            member_count = guild.member_count or 0
            total_members += member_count
            logging.info(f"  - {guild.name} (ID: {guild.id}, 成员: {member_count})")
            guild_count += 1
            
            if not self.command_channel and ALLOWED_COMMAND_CHANNEL_ID != 0:
                 # Attempt to find and cache the command channel in the first available guild
                 # This assumes the command channel ID is the same across all guilds, 
                 # or we primarily care about one specific guild for general bot messages.
                 # If channel ID varies per guild, caching here is less useful.
                 try:
                    channel = guild.get_channel(ALLOWED_COMMAND_CHANNEL_ID) 
                    if channel and isinstance(channel, discord.TextChannel):
                        self.command_channel = channel
                        logging.info(f"Cached command channel: {channel.name} ({channel.id}) in guild {guild.name}")
                    else:
                         logging.warning(f"Could not find or cache command channel {ALLOWED_COMMAND_CHANNEL_ID} in guild {guild.name}.")
                 except discord.Forbidden:
                     logging.error(f"Permission error trying to fetch command channel {ALLOWED_COMMAND_CHANNEL_ID} in guild {guild.name}.")
                 except discord.HTTPException as e:
                     logging.error(f"HTTP error fetching command channel {ALLOWED_COMMAND_CHANNEL_ID} in guild {guild.name}: {e}")

        logging.info(f"📊 总计: {guild_count} 个服务器, {total_members} 个成员")
        await self.bot.change_presence(activity=discord.Game(name="语音统计与TTS"))
        
        # 发送启动消息到命令频道
        if self.command_channel:
            try:
                embed = discord.Embed(
                    title="🚀 机器人已启动",
                    description=f"{self.bot.user.name} 已成功启动并准备就绪！",
                    color=0x00ff00
                )
                embed.add_field(name="服务器数量", value=str(guild_count), inline=True)
                embed.add_field(name="总成员数", value=str(total_members), inline=True)
                embed.add_field(name="延迟", value=f"{round(self.bot.latency * 1000)}ms", inline=True)
                await self.command_channel.send(embed=embed)
            except Exception as e:
                logging.error(f"发送启动消息失败: {e}")

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

    @commands.command(name='help', aliases=['帮助'])
    async def help_command(self, ctx):
        """显示机器人帮助信息"""
        embed = discord.Embed(
            title="🤖 Discord TTS Bot 帮助",
            description="语音统计与文字转语音机器人",
            color=0x00ff00
        )
        
        embed.add_field(
            name="📊 统计命令",
            value="`!stats` - 显示语音统计\n`!relationships` - 显示成员关系网络\n`!heatmap` - 显示共同在线热力图",
            inline=False
        )
        
        embed.add_field(
            name="🔊 TTS命令",
            value="`!say <文本>` - 文字转语音\n`!leave_voice` - 离开语音频道",
            inline=False
        )
        
        embed.add_field(
            name="🔧 其他命令",
            value="`!check_nickname` - 检查昵称\n`!help` - 显示此帮助信息",
            inline=False
        )
        
        embed.set_footer(text="使用 !<命令名> 来执行命令")
        
        await ctx.send(embed=embed)

async def setup(bot):
    """Setup function required for cog loading."""
    await bot.add_cog(EventsCog(bot))