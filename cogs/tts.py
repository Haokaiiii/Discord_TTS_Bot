import discord
from discord.ext import commands, tasks
import logging
import asyncio
import os
import hashlib
import time
from typing import Dict, Optional, Set, Tuple
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from gtts import gTTS
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager

from utils.config import (
    TTS_CACHE_DIR, FFMPEG_EXECUTABLE, EXCLUDED_VOICE_CHANNEL_IDS,
    DEBOUNCE_TIME, TTS_QUEUE_SIZE, TTS_TIMEOUT
)
from utils.helpers import get_preferred_name, has_required_permissions, BotException

# Thread pool for blocking gTTS generation
tts_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="TTS")

@dataclass
class GuildTTSState:
    """Manages TTS state for a single guild."""
    guild_id: int
    voice_client: Optional[discord.VoiceClient] = None
    tts_queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=TTS_QUEUE_SIZE))
    processing_task: Optional[asyncio.Task] = None
    is_reconnecting: bool = False
    connection_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    debounce_timers: Dict[int, asyncio.TimerHandle] = field(default_factory=dict)
    
    def cancel_debounce_timer(self, member_id: int) -> bool:
        """Cancel a debounce timer if it exists."""
        timer = self.debounce_timers.pop(member_id, None)
        if timer:
            timer.cancel()
            return True
        return False
    
    def clear_all_timers(self):
        """Cancel all debounce timers."""
        for timer in self.debounce_timers.values():
            timer.cancel()
        self.debounce_timers.clear()

class TTSCog(commands.Cog):
    """Text-to-speech functionality with improved state management."""
    
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.guild_states: Dict[int, GuildTTSState] = {}
        self.special_user_id = 647282381841104897
        self._tts_cache: Set[str] = set()  # Track cached files
        
        # Start background tasks
        self.cleanup_tts_files.start()
        self.check_voice_connections.start()
        self.populate_cache()
        
        logging.info("TTSCog initialized")
        
    def cog_unload(self):
        """Clean up when cog is unloaded."""
        logging.info("TTSCog unloading...")
        
        # Cancel all tasks
        self.cleanup_tts_files.cancel()
        self.check_voice_connections.cancel()
        
        # Clean up each guild state
        for guild_state in self.guild_states.values():
            guild_state.clear_all_timers()
            if guild_state.processing_task:
                guild_state.processing_task.cancel()
            if guild_state.voice_client:
                asyncio.create_task(self._disconnect_voice(guild_state))
        
        # Shutdown thread pool
        tts_executor.shutdown(wait=False)
        
        logging.info("TTSCog unloaded")
    
    def populate_cache(self):
        """Populate the cache set with existing TTS files."""
        try:
            if os.path.exists(TTS_CACHE_DIR):
                for filename in os.listdir(TTS_CACHE_DIR):
                    if filename.endswith('.mp3'):
                        self._tts_cache.add(filename[:-4])  # Remove .mp3 extension
                logging.info(f"Populated TTS cache with {len(self._tts_cache)} existing files")
        except Exception as e:
            logging.error(f"Error populating TTS cache: {e}")
    
    def get_guild_state(self, guild_id: int) -> GuildTTSState:
        """Get or create guild state."""
        if guild_id not in self.guild_states:
            self.guild_states[guild_id] = GuildTTSState(guild_id)
        return self.guild_states[guild_id]
    
    @commands.Cog.listener()
    async def on_voice_state_update(
        self, 
        member: discord.Member, 
        before: discord.VoiceState, 
        after: discord.VoiceState
    ):
        """Handle voice state changes with debouncing."""
        if member.bot:
            return
            
        guild_state = self.get_guild_state(member.guild.id)
        
        # Determine the action and target channel
        action, target_channel = self._determine_voice_action(before, after)
        
        if not action or not target_channel:
            return
            
        # Cancel any existing timer for this member
        guild_state.cancel_debounce_timer(member.id)
        
        # Schedule debounced announcement
        logging.info(
            f"Scheduling {action} announcement for {get_preferred_name(member)} "
            f"in {target_channel.name} after {DEBOUNCE_TIME}s"
        )
        
        timer = asyncio.get_event_loop().call_later(
            DEBOUNCE_TIME,
            lambda: asyncio.create_task(
                self._process_voice_change(member, action, target_channel)
            )
        )
        
        guild_state.debounce_timers[member.id] = timer
        
        # Check if we should disconnect from the previous channel
        if before.channel and before.channel.id not in EXCLUDED_VOICE_CHANNEL_IDS:
            await self._check_disconnect_empty_channel(guild_state, before.channel)
    
    def _determine_voice_action(
        self, 
        before: discord.VoiceState, 
        after: discord.VoiceState
    ) -> Tuple[Optional[str], Optional[discord.VoiceChannel]]:
        """Determine the voice action type and target channel."""
        before_channel = before.channel if before.channel and before.channel.id not in EXCLUDED_VOICE_CHANNEL_IDS else None
        after_channel = after.channel if after.channel and after.channel.id not in EXCLUDED_VOICE_CHANNEL_IDS else None
        
        if not before_channel and after_channel:
            return 'join', after_channel
        elif before_channel and not after_channel:
            return 'leave', before_channel
        elif before_channel and after_channel and before_channel != after_channel:
            return 'switch', after_channel
            
        return None, None
    
    async def _process_voice_change(
        self, 
        member: discord.Member, 
        action: str, 
        target_channel: discord.VoiceChannel
    ):
        """Process a debounced voice change."""
        guild_state = self.get_guild_state(member.guild.id)
        guild_state.debounce_timers.pop(member.id, None)
        
        # Verify member is still in expected state
        current_channel = member.voice.channel if member.voice else None
        
        if action in ['join', 'switch']:
            if current_channel != target_channel:
                logging.info(f"Member {get_preferred_name(member)} no longer in expected channel, skipping {action} announcement")
                return
        elif action == 'leave':
            if current_channel and current_channel.id not in EXCLUDED_VOICE_CHANNEL_IDS:
                logging.info(f"Member {get_preferred_name(member)} rejoined a channel, skipping leave announcement")
                return
        
        # Check permissions
        if not has_required_permissions(target_channel):
            logging.warning(f"Missing permissions in {target_channel.name}, skipping announcement")
            return
        
        # Generate TTS text
        member_name = get_preferred_name(member)
        
        if action == 'join':
            if member.id == self.special_user_id:
                text = f"欢迎我的主人{member_name}, muamuamua"
            else:
                text = f"欢迎 {member_name}"
        elif action == 'leave':
            text = f"{member_name} 滚了"
        elif action == 'switch':
            text = f"{member_name} 叛变了"
        else:
            return
        
        # Queue the TTS
        await self._queue_tts(member.guild, target_channel, text)
    
    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        """Announce nickname changes."""
        if (before.nick != after.nick and 
            after.voice and 
            after.voice.channel and 
            not after.bot and
            after.voice.channel.id not in EXCLUDED_VOICE_CHANNEL_IDS):
            
            if not has_required_permissions(after.voice.channel):
                return
                
            name_before = get_preferred_name(before)
            name_after = get_preferred_name(after)
            text = f'{name_before} 改名为 {name_after}'
            
            await self._queue_tts(after.guild, after.voice.channel, text)
    
    async def _queue_tts(
        self, 
        guild: discord.Guild, 
        channel: discord.VoiceChannel, 
        text: str
    ):
        """Queue a TTS message for playback."""
        guild_state = self.get_guild_state(guild.id)
        
        try:
            # Create queue item
            item = (channel, text)
            await guild_state.tts_queue.put(item)
            
            logging.debug(f"Queued TTS for guild {guild.id}: '{text[:30]}...'")
            
            # Ensure processor is running
            if not guild_state.processing_task or guild_state.processing_task.done():
                guild_state.processing_task = asyncio.create_task(
                    self._process_tts_queue(guild_state)
                )
                
        except asyncio.QueueFull:
            logging.warning(f"TTS queue full for guild {guild.id}, dropping: '{text[:30]}...'")
    
    async def _process_tts_queue(self, guild_state: GuildTTSState):
        """Process TTS queue for a guild."""
        guild = self.bot.get_guild(guild_state.guild_id)
        if not guild:
            return
            
        logging.info(f"Started TTS processor for guild {guild.id}")
        
        try:
            while True:
                # Get next item
                channel, text = await guild_state.tts_queue.get()
                
                try:
                    # Refresh channel state
                    channel = self.bot.get_channel(channel.id)
                    if not channel or not isinstance(channel, discord.VoiceChannel):
                        continue
                    
                    # Check if channel has humans
                    if not any(not m.bot for m in channel.members):
                        logging.info(f"Channel {channel.name} is empty, skipping TTS")
                        continue
                    
                    # Generate or get cached TTS file
                    tts_path = await self._get_tts_file(text)
                    if not tts_path:
                        logging.error(f"Failed to generate TTS for: '{text[:30]}...'")
                        continue
                    
                    # Get or connect voice client
                    vc = await self._ensure_voice_connection(guild_state, channel)
                    if not vc:
                        logging.error(f"Failed to connect to {channel.name}")
                        continue
                    
                    # Play the TTS
                    await self._play_tts(guild_state, vc, tts_path)
                    
                finally:
                    guild_state.tts_queue.task_done()
                    
        except asyncio.CancelledError:
            logging.info(f"TTS processor cancelled for guild {guild.id}")
            raise
        except Exception as e:
            logging.error(f"Error in TTS processor for guild {guild.id}: {e}", exc_info=True)
            await asyncio.sleep(1)  # Brief pause before potential restart
    
    async def _get_tts_file(self, text: str) -> Optional[str]:
        """Generate or retrieve cached TTS file."""
        # Generate hash for caching
        text_hash = hashlib.md5(text.encode('utf-8')).hexdigest()
        output_path = os.path.join(TTS_CACHE_DIR, f"{text_hash}.mp3")
        
        # Check cache
        if text_hash in self._tts_cache and os.path.exists(output_path):
            logging.debug(f"Using cached TTS: {text_hash}")
            return output_path
        
        # Generate new TTS
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                tts_executor,
                self._generate_tts_file,
                text,
                output_path
            )
            
            self._tts_cache.add(text_hash)
            logging.info(f"Generated TTS: {text_hash}")
            return output_path
            
        except Exception as e:
            logging.error(f"Failed to generate TTS: {e}")
            return None
    
    def _generate_tts_file(self, text: str, output_path: str):
        """Generate TTS file (blocking operation for thread pool)."""
        temp_path = output_path + '.tmp'
        
        try:
            tts = gTTS(text=text, lang='zh-CN')
            tts.save(temp_path)
            os.replace(temp_path, output_path)  # Atomic operation
            
        except Exception:
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    pass
            raise
    
    @asynccontextmanager
    async def _voice_operation(self, guild_state: GuildTTSState):
        """Context manager for voice operations with locking."""
        async with guild_state.connection_lock:
            yield
    
    async def _ensure_voice_connection(
        self, 
        guild_state: GuildTTSState, 
        channel: discord.VoiceChannel
    ) -> Optional[discord.VoiceClient]:
        """Ensure we're connected to the correct voice channel."""
        async with self._voice_operation(guild_state):
            vc = guild_state.voice_client
            
            # Check existing connection
            if vc and vc.is_connected():
                if vc.channel == channel:
                    return vc
                    
                # Move to new channel
                try:
                    await vc.move_to(channel)
                    logging.info(f"Moved to {channel.name} in guild {channel.guild.id}")
                    return vc
                except Exception as e:
                    logging.error(f"Failed to move voice client: {e}")
                    await self._disconnect_voice(guild_state)
            
            # Connect to channel
            if guild_state.is_reconnecting:
                logging.warning(f"Already reconnecting in guild {channel.guild.id}")
                return None
                
            guild_state.is_reconnecting = True
            
            try:
                vc = await channel.connect(timeout=10.0, reconnect=True)
                guild_state.voice_client = vc
                logging.info(f"Connected to {channel.name} in guild {channel.guild.id}")
                return vc
                
            except asyncio.TimeoutError:
                logging.error(f"Timeout connecting to {channel.name}")
                return None
            except discord.ClientException as e:
                logging.error(f"Failed to connect: {e}")
                return None
            except Exception as e:
                logging.error(f"Unexpected error connecting: {e}", exc_info=True)
                return None
            finally:
                guild_state.is_reconnecting = False
    
    async def _play_tts(
        self, 
        guild_state: GuildTTSState, 
        vc: discord.VoiceClient, 
        tts_path: str
    ):
        """Play TTS file with proper synchronization."""
        async with self._voice_operation(guild_state):
            if not vc.is_connected():
                return
                
            if vc.is_playing():
                logging.debug("Already playing, skipping")
                return
            
            # Create completion event
            done = asyncio.Event()
            
            def after_playback(error):
                if error:
                    logging.error(f"Playback error: {error}")
                asyncio.run_coroutine_threadsafe(done.set(), asyncio.get_event_loop())
            
            try:
                # Create audio source and play
                source = discord.FFmpegPCMAudio(tts_path, executable=FFMPEG_EXECUTABLE)
                vc.play(source, after=after_playback)
                
                # Wait for completion with timeout
                await asyncio.wait_for(done.wait(), timeout=TTS_TIMEOUT)
                
            except asyncio.TimeoutError:
                logging.error("Playback timeout")
                if vc.is_playing():
                    vc.stop()
            except Exception as e:
                logging.error(f"Playback error: {e}", exc_info=True)
                if vc.is_playing():
                    vc.stop()
            
            # Check if we should disconnect
            await self._check_disconnect_empty_channel(guild_state, vc.channel)
    
    async def _check_disconnect_empty_channel(
        self, 
        guild_state: GuildTTSState, 
        channel: discord.VoiceChannel
    ):
        """Disconnect if channel only has the bot."""
        vc = guild_state.voice_client
        if not vc or not vc.is_connected() or vc.channel != channel:
            return
        
        # Refresh channel state
        channel = self.bot.get_channel(channel.id)
        if not channel:
            await self._disconnect_voice(guild_state)
            return
        
        # Check for humans
        if not any(not m.bot for m in channel.members):
            logging.info(f"Channel {channel.name} is empty, disconnecting")
            await self._disconnect_voice(guild_state)
    
    async def _disconnect_voice(self, guild_state: GuildTTSState):
        """Disconnect from voice channel."""
        async with self._voice_operation(guild_state):
            vc = guild_state.voice_client
            if vc:
                try:
                    if vc.is_playing():
                        vc.stop()
                    await asyncio.wait_for(vc.disconnect(force=True), timeout=5.0)
                    logging.info(f"Disconnected from voice in guild {guild_state.guild_id}")
                except Exception as e:
                    logging.error(f"Error disconnecting: {e}")
                finally:
                    guild_state.voice_client = None
    
    @tasks.loop(hours=1)
    async def cleanup_tts_files(self):
        """Clean up old TTS cache files."""
        logging.info("Starting TTS cache cleanup")
        
        try:
            now = time.time()
            cutoff = now - (24 * 3600)  # 24 hours
            removed = 0
            
            for filename in os.listdir(TTS_CACHE_DIR):
                if not filename.endswith('.mp3'):
                    continue
                    
                filepath = os.path.join(TTS_CACHE_DIR, filename)
                
                try:
                    if os.path.getmtime(filepath) < cutoff:
                        os.remove(filepath)
                        # Remove from cache set
                        file_hash = filename[:-4]
                        self._tts_cache.discard(file_hash)
                        removed += 1
                except OSError:
                    continue
            
            logging.info(f"Cleaned up {removed} old TTS files")
            
        except Exception as e:
            logging.error(f"Error during TTS cleanup: {e}", exc_info=True)
    
    @cleanup_tts_files.before_loop
    async def before_cleanup(self):
        await self.bot.wait_until_ready()
    
    @tasks.loop(minutes=5)
    async def check_voice_connections(self):
        """Periodically check and clean up voice connections."""
        logging.debug("Checking voice connections")
        
        for guild_state in list(self.guild_states.values()):
            vc = guild_state.voice_client
            if not vc:
                continue
                
            # Check if still connected
            if not vc.is_connected():
                logging.warning(f"Found disconnected VC for guild {guild_state.guild_id}")
                guild_state.voice_client = None
                continue
            
            # Check if channel exists and has humans
            channel = self.bot.get_channel(vc.channel.id)
            if not channel or not any(not m.bot for m in channel.members):
                logging.info(f"Found bot alone in {vc.channel.name}, disconnecting")
                await self._disconnect_voice(guild_state)
    
    @check_voice_connections.before_loop
    async def before_check_connections(self):
        await self.bot.wait_until_ready()
    
    # Commands
    
    @commands.command(name='say')
    @commands.has_permissions(manage_messages=True)
    async def say_tts(self, ctx: commands.Context, *, text: str):
        """Make the bot say text in your voice channel."""
        if not ctx.author.voice or not ctx.author.voice.channel:
            await ctx.send("您需要在语音频道中才能使用此命令。", delete_after=10)
            return
        
        channel = ctx.author.voice.channel
        if channel.id in EXCLUDED_VOICE_CHANNEL_IDS:
            await ctx.send("此语音频道已禁用TTS。", delete_after=10)
            return
        
        if not has_required_permissions(channel):
            await ctx.send("我没有权限加入或在您的语音频道中说话。", delete_after=10)
            return
        
        await self._queue_tts(ctx.guild, channel, text)
        await ctx.message.add_reaction('🔊')
    
    @commands.command(name='leave_voice')
    @commands.has_permissions(manage_guild=True)
    async def leave_voice(self, ctx: commands.Context):
        """Force the bot to leave voice channel."""
        guild_state = self.guild_states.get(ctx.guild.id)
        
        if guild_state and guild_state.voice_client and guild_state.voice_client.is_connected():
            channel_name = guild_state.voice_client.channel.name
            await self._disconnect_voice(guild_state)
            await ctx.send(f"已离开语音频道 {channel_name}")
        else:
            await ctx.send("我当前不在此服务器的任何语音频道中。", delete_after=10)

async def setup(bot: commands.Bot):
    await bot.add_cog(TTSCog(bot)) 