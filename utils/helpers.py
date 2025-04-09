import discord
from discord.ext import commands

def get_preferred_name(member: discord.Member | discord.User) -> str:
    """Returns the nickname if available, otherwise the global name or username."""
    if isinstance(member, discord.Member) and member.nick:
        return member.nick
    if hasattr(member, 'global_name') and member.global_name:
        return member.global_name
    return member.name

def check_channel():
    """Decorator to check if the command is used in the allowed channel."""
    async def predicate(ctx: commands.Context):
        from utils.config import ALLOWED_COMMAND_CHANNEL_ID
        # Allow DMs or specific channel
        if isinstance(ctx.channel, discord.DMChannel):
            return True
        if ALLOWED_COMMAND_CHANNEL_ID == 0:
            return True # Allow if no channel is specified
        if ctx.channel.id == ALLOWED_COMMAND_CHANNEL_ID:
            return True
        await ctx.send(f"命令只能在指定的频道 <#{ALLOWED_COMMAND_CHANNEL_ID}> 或私信中使用。", delete_after=10)
        return False
    return commands.check(predicate)

def has_required_permissions(channel: discord.VoiceChannel) -> bool:
    """Check if the bot has connect and speak permissions in the voice channel."""
    permissions = channel.permissions_for(channel.guild.me)
    return permissions.connect and permissions.speak

async def send_to_command_channel(bot: commands.Bot, guild_id: int, content: str = None, file: discord.File = None, embed: discord.Embed = None):
    """Sends a message, file, or embed to the designated command channel for a guild."""
    from utils.config import ALLOWED_COMMAND_CHANNEL_ID

    if ALLOWED_COMMAND_CHANNEL_ID == 0:
        logging.warning(f"Command channel not configured for guild {guild_id}. Cannot send message.")
        return

    guild = bot.get_guild(guild_id)
    if not guild:
        logging.warning(f"Could not find guild {guild_id}. Cannot send message.")
        return

    channel = guild.get_channel(ALLOWED_COMMAND_CHANNEL_ID)
    if not channel or not isinstance(channel, discord.TextChannel):
        # Fallback: Try to find the first available text channel the bot can write to
        fallback_channel = None
        for ch in guild.text_channels:
            if ch.permissions_for(guild.me).send_messages:
                fallback_channel = ch
                break
        if fallback_channel:
             logging.warning(f"Designated command channel {ALLOWED_COMMAND_CHANNEL_ID} not found or invalid in guild {guild_id}. Sending to fallback {fallback_channel.name} ({fallback_channel.id}).")
             channel = fallback_channel
        else:
             logging.error(f"Designated command channel {ALLOWED_COMMAND_CHANNEL_ID} not found or invalid, and no fallback channel found in guild {guild_id}. Cannot send message.")
             return

    try:
        await channel.send(content=content, file=file, embed=embed)
        logging.info(f"Sent message to command channel {channel.name} ({channel.id}) in guild {guild_id}")
    except discord.Forbidden:
        logging.error(f"Missing permissions to send messages in channel {channel.name} ({channel.id}) in guild {guild_id}.")
    except discord.HTTPException as e:
        logging.error(f"Failed to send message to command channel {channel.name} ({channel.id}) in guild {guild_id}: {e}")

class BotException(Exception):
    """Custom exception class for bot-specific errors."""
    pass

async def handle_command_error(ctx: commands.Context, error):
    """Global error handler for commands."""
    if isinstance(error, commands.CommandNotFound):
        # await ctx.send("未知命令。", delete_after=10)
        return # Ignore unknown commands silently
    elif isinstance(error, commands.CheckFailure):
        # Handled by the check_channel decorator usually
        logging.warning(f"Check failed for command '{ctx.command}' by {ctx.author}: {error}")
        pass # Message is sent by the check itself
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"缺少必要的参数: {error.param.name}", delete_after=10)
    elif isinstance(error, commands.BadArgument):
        await ctx.send("参数类型错误或无效。", delete_after=10)
    elif isinstance(error, BotException):
        await ctx.send(f"发生错误: {error}", delete_after=10)
    elif isinstance(error, commands.CommandInvokeError):
        original = error.original
        if isinstance(original, discord.Forbidden):
            await ctx.send("机器人缺少执行此操作所需的权限。")
        elif isinstance(original, discord.HTTPException):
            await ctx.send(f"网络错误: {original.status} {original.text}")
        else:
            logging.error(f"Command '{ctx.command}' raised an exception: {original}", exc_info=original)
            await ctx.send("执行命令时发生内部错误。")
    else:
        logging.error(f"Unhandled error in command '{ctx.command}': {error}", exc_info=error)
        await ctx.send("发生未知错误。") 