"""Helper utilities for Discord bot command handling and safety.

Includes channel permission checks, safe send helpers, and user-friendly
error handling. All public APIs are documented with NumPy-style docstrings.
"""
import logging
from typing import Optional, Union
import time
import discord
from discord.ext import commands
from functools import lru_cache

# Cache for command channel lookups
_command_channel_cache: dict[int, Optional[discord.TextChannel]] = {}
_command_channel_cache_ts: dict[int, float] = {}
_COMMAND_CHANNEL_TTL_SECONDS = 600.0  # 10 minutes

# Simple per-guild rate limiter for sends (token bucket)
_SEND_RATE_LIMIT_TOKENS_PER_SEC = 1.0
_SEND_RATE_LIMIT_BURST = 3.0
_guild_tokens: dict[int, float] = {}
_guild_last_refill: dict[int, float] = {}


def _rate_limited(guild_id: int) -> bool:
    now = time.monotonic()
    capacity = _SEND_RATE_LIMIT_BURST
    rate = _SEND_RATE_LIMIT_TOKENS_PER_SEC

    last = _guild_last_refill.get(guild_id, now)
    tokens = _guild_tokens.get(guild_id, capacity)

    # Refill
    tokens = min(capacity, tokens + (now - last) * rate)

    if tokens >= 1.0:
        tokens -= 1.0
        _guild_tokens[guild_id] = tokens
        _guild_last_refill[guild_id] = now
        return False  # not limited

    # Store updated state even when limited
    _guild_tokens[guild_id] = tokens
    _guild_last_refill[guild_id] = now
    return True

def get_preferred_name(member: Union[discord.Member, discord.User]) -> str:
    """Return a member's preferred display name.

    Parameters
    ----------
    member : discord.Member or discord.User
        Discord entity to derive a display name from.

    Returns
    -------
    str
        Nickname if set, otherwise global name or username.
    """
    if isinstance(member, discord.Member) and member.nick:
        return member.nick
    if hasattr(member, 'global_name') and member.global_name:
        return member.global_name
    return member.name

def check_channel():
    """Restrict a command to the configured channel or DMs.

    Returns
    -------
    Callable
        A command check decorator that validates the channel context.
    """
    async def predicate(ctx: commands.Context) -> bool:
        from utils.config import ALLOWED_COMMAND_CHANNEL_ID
        
        # Allow DMs
        if isinstance(ctx.channel, discord.DMChannel):
            return True
            
        # Allow any channel if not configured
        if ALLOWED_COMMAND_CHANNEL_ID == 0:
            return True
            
        # Check if in allowed channel
        if ctx.channel.id == ALLOWED_COMMAND_CHANNEL_ID:
            return True
            
        # Send error message
        try:
            await ctx.send(
                f"命令只能在指定的频道 <#{ALLOWED_COMMAND_CHANNEL_ID}> 或私信中使用。",
                delete_after=10
            )
        except discord.HTTPException:
            logging.warning(f"Failed to send channel restriction message in {ctx.channel.id}")
            
        return False
        
    return commands.check(predicate)

def has_required_permissions(channel: discord.VoiceChannel) -> bool:
    """Check if the bot has connect and speak permissions in a voice channel.

    Parameters
    ----------
    channel : discord.VoiceChannel
        The voice channel to check permissions for.

    Returns
    -------
    bool
        True when both connect and speak permissions are present.
    """
    if not channel.guild.me:
        logging.error(f"Bot member not found in guild {channel.guild.id}")
        return False
        
    permissions = channel.permissions_for(channel.guild.me)
    return permissions.connect and permissions.speak

@lru_cache(maxsize=128)
def _get_command_channel(guild: discord.Guild, channel_id: int) -> Optional[discord.TextChannel]:
    """Get the configured command channel with caching.

    Parameters
    ----------
    guild : discord.Guild
        Guild to search in.
    channel_id : int
        Channel identifier.

    Returns
    -------
    discord.TextChannel or None
        The channel if found and valid, otherwise None.
    """
    channel = guild.get_channel(channel_id)
    if channel and isinstance(channel, discord.TextChannel):
        return channel
    return None

def _find_fallback_channel(guild: discord.Guild) -> Optional[discord.TextChannel]:
    """Find a fallback text channel the bot can write to.

    Parameters
    ----------
    guild : discord.Guild
        Guild to search in.

    Returns
    -------
    discord.TextChannel or None
        First available text channel with send permissions, or None.
    """
    bot_member = guild.me
    if not bot_member:
        return None
        
    # Prioritize channels based on common patterns
    priority_patterns = ['general', 'chat', 'bot', 'command']
    
    # First, try priority channels
    for pattern in priority_patterns:
        for channel in guild.text_channels:
            if pattern in channel.name.lower() and channel.permissions_for(bot_member).send_messages:
                return channel
    
    # Then, try any channel we can write to
    for channel in guild.text_channels:
        if channel.permissions_for(bot_member).send_messages:
            return channel
            
    return None

async def send_to_command_channel(
    bot: commands.Bot,
    guild_id: int,
    content: Optional[str] = None,
    file: Optional[discord.File] = None,
    embed: Optional[discord.Embed] = None
) -> bool:
    """Send content to the configured command channel for a guild.

    Parameters
    ----------
    bot : commands.Bot
        The bot instance.
    guild_id : int
        Guild ID to send to.
    content : str or None, optional
        Text content to send.
    file : discord.File or None, optional
        File attachment to send.
    embed : discord.Embed or None, optional
        Rich embed to send.

    Returns
    -------
    bool
        True if the message was sent successfully, False otherwise.
    """
    from utils.config import ALLOWED_COMMAND_CHANNEL_ID

    # Validate inputs
    if not any([content, file, embed]):
        logging.warning("send_to_command_channel called with no content")
        return False

    # Rate limit per guild
    if _rate_limited(guild_id):
        logging.warning(f"Rate limited send_to_command_channel for guild {guild_id}")
        return False

    guild = bot.get_guild(guild_id)
    if not guild:
        logging.warning(f"Could not find guild {guild_id}. Cannot send message.")
        return False

    # Try to get cached channel first (with TTL)
    channel = _command_channel_cache.get(guild_id)
    ts = _command_channel_cache_ts.get(guild_id, 0.0)
    if channel and (time.monotonic() - ts) > _COMMAND_CHANNEL_TTL_SECONDS:
        # Expired
        channel = None
        _command_channel_cache.pop(guild_id, None)
    
    # If not cached or invalid, find channel
    if not channel or not isinstance(channel, discord.TextChannel):
        channel = None
        
        if ALLOWED_COMMAND_CHANNEL_ID != 0:
            channel = _get_command_channel(guild, ALLOWED_COMMAND_CHANNEL_ID)
            
            if not channel:
                logging.warning(
                    f"Designated command channel {ALLOWED_COMMAND_CHANNEL_ID} not found "
                    f"or invalid in guild {guild_id}. Looking for fallback channel."
                )
        else:
            logging.info(f"No command channel configured for guild {guild_id}. Looking for fallback channel.")
        
        # Find fallback if needed
        if not channel:
            channel = _find_fallback_channel(guild)
            
            if channel:
                logging.info(f"Using fallback channel {channel.name} ({channel.id}) in guild {guild_id}.")
            else:
                logging.error(f"No suitable channel found in guild {guild_id}. Cannot send message.")
                return False
        
        # Cache the channel with timestamp
        _command_channel_cache[guild_id] = channel
        _command_channel_cache_ts[guild_id] = time.monotonic()

    # Send the message
    try:
        await channel.send(content=content, file=file, embed=embed)
        logging.info(f"Sent message to channel {channel.name} ({channel.id}) in guild {guild_id}")
        return True
        
    except discord.Forbidden:
        logging.error(f"Missing permissions to send messages in channel {channel.name} ({channel.id}) in guild {guild_id}.")
        # Clear cache entry as channel might have changed permissions
        _command_channel_cache.pop(guild_id, None)
        _command_channel_cache_ts.pop(guild_id, None)
        
    except discord.HTTPException as e:
        logging.error(f"Failed to send message to channel {channel.name} ({channel.id}) in guild {guild_id}: {e}")
        
    except Exception as e:
        logging.error(f"Unexpected error sending message to guild {guild_id}: {e}", exc_info=True)
        
    return False

def clear_channel_cache(guild_id: Optional[int] = None):
    """Clear the command channel cache.

    Parameters
    ----------
    guild_id : int or None, optional
        If provided, only clear cache for this guild. Otherwise clear all.
    """
    if guild_id:
        _command_channel_cache.pop(guild_id, None)
        _command_channel_cache_ts.pop(guild_id, None)
        _get_command_channel.cache_clear()  # Clear LRU cache as well
    else:
        _command_channel_cache.clear()
        _command_channel_cache_ts.clear()
        _get_command_channel.cache_clear()

class BotException(Exception):
    """Custom exception class for bot-specific errors."""
    pass

async def handle_command_error(ctx: commands.Context, error: Exception):
    """Global error handler for commands with improved messages.

    Parameters
    ----------
    ctx : commands.Context
        The command context.
    error : Exception
        The exception that was raised.
    """
    # Log the error first
    if ctx.command:
        logging.error(f"Error in command '{ctx.command.name}' by {ctx.author}: {error}", exc_info=error)
    
    # Handle specific error types
    if isinstance(error, commands.CommandNotFound):
        # Silently ignore unknown commands
        return
        
    elif isinstance(error, commands.CheckFailure):
        # Already handled by check decorators usually
        logging.debug(f"Check failed for command '{ctx.command}' by {ctx.author}: {error}")
        return
        
    elif isinstance(error, commands.MissingRequiredArgument):
        await safe_send(ctx, f"缺少必要的参数: {error.param.name}", delete_after=10)
        
    elif isinstance(error, commands.BadArgument):
        await safe_send(ctx, f"参数错误: {str(error)}", delete_after=10)
        
    elif isinstance(error, commands.CommandOnCooldown):
        await safe_send(ctx, f"命令冷却中，请 {error.retry_after:.1f} 秒后再试。", delete_after=10)
        
    elif isinstance(error, BotException):
        await safe_send(ctx, f"发生错误: {error}", delete_after=15)
        
    elif isinstance(error, commands.CommandInvokeError):
        original = error.original
        
        if isinstance(original, discord.Forbidden):
            await safe_send(ctx, "机器人缺少执行此操作所需的权限。")
            
        elif isinstance(original, discord.HTTPException):
            if original.status == 429:  # Rate limited
                await safe_send(ctx, "请求过于频繁，请稍后再试。")
            else:
                await safe_send(ctx, f"网络错误: {original.status} {original.text[:100]}")
                
        else:
            await safe_send(ctx, "执行命令时发生内部错误，已记录详情。")
            
    else:
        await safe_send(ctx, "发生未知错误，已记录详情。")

async def safe_send(
    ctx: commands.Context,
    content: str,
    delete_after: Optional[float] = None,
    **kwargs
) -> Optional[discord.Message]:
    """Safely send a message with error handling.

    Parameters
    ----------
    ctx : commands.Context
        The command context.
    content : str
        The message content.
    delete_after : float or None, optional
        Seconds after which to delete the message.
    **kwargs
        Additional arguments to pass to ``ctx.send``.

    Returns
    -------
    discord.Message or None
        The sent message if successful, None otherwise.
    """
    try:
        return await ctx.send(content, delete_after=delete_after, **kwargs)
    except discord.HTTPException as e:
        logging.warning(f"Failed to send message in {ctx.channel}: {e}")
        return None
    except Exception as e:
        logging.error(f"Unexpected error sending message: {e}", exc_info=True)
        return None 