import discord
from discord.ext import commands, tasks
import logging
import asyncio
import os
import hashlib
import time
from gtts import gTTS
from concurrent.futures import ThreadPoolExecutor
from collections import defaultdict, deque
from datetime import datetime, timedelta

from utils.config import TTS_CACHE_DIR, FFMPEG_EXECUTABLE, EXCLUDED_VOICE_CHANNEL_IDS, DEBOUNCE_TIME
from utils.helpers import get_preferred_name, has_required_permissions, BotException

# Consider moving locks and shared state to the main bot or a central state manager
# if other cogs need access or if state becomes complex.
guild_voice_clients = {} # {guild_id: discord.VoiceClient}
guild_tts_queues = defaultdict(lambda: deque()) # {guild_id: deque([(voice_channel, message, tts_path)])}
guild_tts_tasks = {} # {guild_id: asyncio.Task}
pending_switch_tasks = {} # {(guild_id, member_id): asyncio.Task}
reconnecting_guilds = set() # {guild_id}
voice_connection_lock = asyncio.Lock() # Global lock for connect/disconnect/play operations

# Thread pool for blocking gTTS generation
tts_executor = ThreadPoolExecutor(max_workers=4)

class TTSCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.cleanup_tts_files.start()
        self.check_voice_connections.start()
        logging.info("TTSCog initialized.")

    def cog_unload(self):
        self.cleanup_tts_files.cancel()
        self.check_voice_connections.cancel()
        tts_executor.shutdown(wait=False) # Don't wait for pending TTS tasks
        # Clean up any running TTS tasks and voice clients
        for task in guild_tts_tasks.values():
            task.cancel()
        for vc in guild_voice_clients.values():
            asyncio.create_task(self._safe_disconnect(vc))
        logging.info("TTSCog unloaded.")

    async def _safe_disconnect(self, vc: discord.VoiceClient):
        """Safely disconnects a voice client, handling potential errors."""
        if vc and vc.is_connected():
            guild_id = vc.guild.id
            logging.info(f"Safely disconnecting from voice channel in guild {guild_id}")
            try:
                async with voice_connection_lock:
                    if vc.is_playing():
                        vc.stop()
                    await vc.disconnect(force=True)
                    logging.info(f"Successfully disconnected from voice in guild {guild_id}")
            except Exception as e:
                logging.error(f"Error during safe disconnect in guild {guild_id}: {e}", exc_info=True)
            finally:
                guild_voice_clients.pop(guild_id, None)
                guild_tts_queues.pop(guild_id, None)
                if guild_id in guild_tts_tasks:
                    guild_tts_tasks[guild_id].cancel()
                    guild_tts_tasks.pop(guild_id, None)
                reconnecting_guilds.discard(guild_id)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        """Handles TTS announcements for voice channel joins, leaves, and switches."""
        if member.bot:
            return

        guild = member.guild
        guild_id = guild.id
        member_id = member.id
        name = get_preferred_name(member)

        before_channel = before.channel
        after_channel = after.channel

        # Ignore events in excluded channels unless they are leaving an excluded channel
        is_before_excluded = before_channel and before_channel.id in EXCLUDED_VOICE_CHANNEL_IDS
        is_after_excluded = after_channel and after_channel.id in EXCLUDED_VOICE_CHANNEL_IDS

        # --- JOIN --- (From None or Excluded to a valid channel)
        if (not before_channel or is_before_excluded) and (after_channel and not is_after_excluded):
            # Cancel any pending switch task for this member
            switch_task = pending_switch_tasks.pop((guild_id, member_id), None)
            if switch_task:
                switch_task.cancel()
                logging.debug(f"Cancelled pending switch task for {name} ({member_id}) due to join.")

            # Check permissions before queueing
            if not has_required_permissions(after_channel):
                 logging.warning(f"Missing permissions to join/speak in {after_channel.name} ({after_channel.id}). Cannot announce join for {name}.")
                 return
            message = f'{name} 加入了频道'
            logging.info(f"Queueing TTS for join: {message} in G:{guild_id} C:{after_channel.id}")
            await self.queue_tts(guild, after_channel, message)

        # --- LEAVE --- (From valid channel to None or Excluded)
        elif (before_channel and not is_before_excluded) and (not after_channel or is_after_excluded):
            # Cancel any pending switch task
            switch_task = pending_switch_tasks.pop((guild_id, member_id), None)
            if switch_task:
                switch_task.cancel()
                logging.debug(f"Cancelled pending switch task for {name} ({member_id}) due to leave.")

            # Check permissions before queueing
            if not has_required_permissions(before_channel):
                 logging.warning(f"Missing permissions in {before_channel.name} ({before_channel.id}) where {name} left. Cannot announce leave.")
                 # Might still want to try and disconnect if bot is alone?
                 if len(before_channel.members) == 1 and before_channel.members[0] == guild.me:
                      logging.info(f"Bot is alone in {before_channel.name}, attempting disconnect.")
                      vc = guild_voice_clients.get(guild_id)
                      if vc and vc.channel == before_channel:
                          await self._safe_disconnect(vc)
                 return

            message = f'{name} 离开了频道'
            logging.info(f"Queueing TTS for leave: {message} in G:{guild_id} C:{before_channel.id}")
            await self.queue_tts(guild, before_channel, message)

            # Check if the bot should disconnect after announcing the leave
            await self.check_and_disconnect_if_alone(guild_id, before_channel)

        # --- SWITCH --- (From valid channel to another valid channel)
        elif (before_channel and not is_before_excluded) and \
             (after_channel and not is_after_excluded) and \
             before_channel != after_channel:

            # If a switch task is already pending, cancel it (user switched again quickly)
            existing_task = pending_switch_tasks.pop((guild_id, member_id), None)
            if existing_task:
                existing_task.cancel()
                logging.debug(f"Cancelled existing switch task for {name} ({member_id}) due to rapid switch.")

            # Check permissions for the *destination* channel
            if not has_required_permissions(after_channel):
                logging.warning(f"Missing permissions in destination channel {after_channel.name} ({after_channel.id}). Cannot announce switch for {name}.")
                # Announce leave in the original channel if possible?
                if has_required_permissions(before_channel):
                     leave_message = f'{name} 离开了频道'
                     logging.info(f"Queueing TTS for leave part of failed switch: {leave_message} in G:{guild_id} C:{before_channel.id}")
                     await self.queue_tts(guild, before_channel, leave_message)
                     await self.check_and_disconnect_if_alone(guild_id, before_channel)
                return

            logging.info(f"Detected switch for {name} ({member_id}) from {before_channel.id} to {after_channel.id}. Starting debounce timer ({DEBOUNCE_TIME}s).")
            # Start a delayed task to announce the switch
            task = asyncio.create_task(self.delayed_switch_broadcast(guild, member, after_channel))
            pending_switch_tasks[(guild_id, member_id)] = task

            # Check if bot needs to leave the *origin* channel
            await self.check_and_disconnect_if_alone(guild_id, before_channel)

    async def delayed_switch_broadcast(self, guild: discord.Guild, member: discord.Member, voice_channel: discord.VoiceChannel):
        """Waits for DEBOUNCE_TIME then queues the switch announcement if still valid."""
        guild_id = guild.id
        member_id = member.id
        name = get_preferred_name(member)
        await asyncio.sleep(DEBOUNCE_TIME)

        # Check if the task was cancelled or the member is no longer in the target channel
        if (guild_id, member_id) not in pending_switch_tasks:
             logging.info(f"Debounced switch task for {name} ({member_id}) cancelled or already handled.")
             return

        # Double check the member's current state *after* the delay
        current_state = member.voice
        if not current_state or current_state.channel != voice_channel:
            logging.info(f"Member {name} ({member_id}) is no longer in {voice_channel.name} after debounce. Aborting switch announcement.")
            pending_switch_tasks.pop((guild_id, member_id), None) # Clean up task entry
            return

        # Check permissions again right before queueing
        if not has_required_permissions(voice_channel):
            logging.warning(f"Missing permissions in {voice_channel.name} ({voice_channel.id}) after debounce. Cannot announce switch for {name}.")
            pending_switch_tasks.pop((guild_id, member_id), None)
            return

        message = f'{name} 切换到了频道'
        logging.info(f"Debounce finished. Queueing TTS for switch: {message} in G:{guild_id} C:{voice_channel.id}")
        await self.queue_tts(guild, voice_channel, message)
        pending_switch_tasks.pop((guild_id, member_id), None) # Clean up task entry

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        """Announces nickname changes via TTS if the user is in a voice channel."""
        if before.nick != after.nick and after.voice and after.voice.channel and not after.bot:
            voice_channel = after.voice.channel
            if voice_channel.id in EXCLUDED_VOICE_CHANNEL_IDS:
                return
            
            guild = after.guild
            name_before = get_preferred_name(before)
            name_after = get_preferred_name(after)
            message = f'{name_before} 改名为 {name_after}'

            # Check permissions before queueing
            if not has_required_permissions(voice_channel):
                logging.warning(f"Missing permissions in {voice_channel.name} ({voice_channel.id}). Cannot announce nickname change for {name_after}.")
                return

            logging.info(f"Queueing TTS for nickname change: {message} in G:{guild.id} C:{voice_channel.id}")
            await self.queue_tts(guild, voice_channel, message)

    async def queue_tts(self, guild: discord.Guild, voice_channel: discord.VoiceChannel, message: str):
        """Adds a TTS message to the guild's queue and ensures the processing task is running."""
        guild_id = guild.id
        if not message:
            return

        # Generate TTS file path (hash message for caching)
        text_hash = hashlib.md5(message.encode('utf-8')).hexdigest()
        output_filename = f"{text_hash}.mp3"
        output_path = os.path.join(TTS_CACHE_DIR, output_filename)

        # Add to queue
        guild_tts_queues[guild_id].append((voice_channel, message, output_path))
        logging.debug(f"Added to TTS queue for guild {guild_id}: '{message[:30]}...' Target: {voice_channel.name}")

        # Ensure the processing task for this guild is running
        if guild_id not in guild_tts_tasks or guild_tts_tasks[guild_id].done():
            logging.info(f"Starting TTS processing task for guild {guild_id}.")
            guild_tts_tasks[guild_id] = asyncio.create_task(self.process_guild_tts_queue(guild_id))
            # Handle task completion/exceptions
            guild_tts_tasks[guild_id].add_done_callback(self._handle_tts_task_completion)

    def _handle_tts_task_completion(self, task: asyncio.Task):
        """Callback to log exceptions from the TTS processing task."""
        try:
            task.result() # Raise exception if one occurred
        except asyncio.CancelledError:
            logging.info(f"TTS processing task cancelled.") # Expected during shutdown/reloads
        except Exception as e:
            # Find guild ID associated with this task (if possible)
            guild_id = None
            for gid, t in guild_tts_tasks.items():
                if t == task:
                    guild_id = gid
                    break
            logging.error(f"TTS processing task for guild {guild_id or 'Unknown'} failed: {e}", exc_info=True)
            # Optionally, try restarting the task after a delay?
            if guild_id:
                 guild_tts_tasks.pop(guild_id, None) # Remove the failed task entry


    async def generate_tts_async(self, text: str, output_path: str):
        """Generates TTS audio file using gTTS in a ThreadPoolExecutor."""
        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(
                tts_executor,
                self._blocking_gtts_call, # Run the blocking function
                text,
                output_path
            )
            logging.info(f"Successfully generated TTS file: {output_path}")
            return True
        except Exception as e:
            logging.error(f"Failed to generate TTS for '{text[:30]}...': {e}", exc_info=True)
            # Attempt to clean up potentially partial file
            try:
                 if os.path.exists(output_path):
                     os.remove(output_path)
            except OSError as oe:
                 logging.error(f"Error removing failed TTS file {output_path}: {oe}")
            return False

    def _blocking_gtts_call(self, text: str, output_path: str):
        """The actual blocking gTTS call. DO NOT run this directly in the main event loop."""
        tts = gTTS(text=text, lang='zh-cn')
        tts.save(output_path)


    async def process_guild_tts_queue(self, guild_id: int):
        """Processes the TTS queue for a specific guild, one message at a time."""
        guild = self.bot.get_guild(guild_id)
        if not guild:
            logging.error(f"Cannot process TTS queue: Guild {guild_id} not found.")
            guild_tts_queues.pop(guild_id, None)
            return

        while guild_id in guild_tts_queues and guild_tts_queues[guild_id]:
            try:
                voice_channel, message, tts_path = guild_tts_queues[guild_id].popleft()
                logging.info(f"Processing TTS for guild {guild_id}: '{message[:30]}...' in {voice_channel.name}")

                # 0. Check if channel still exists and has members (besides bot)
                refreshed_channel = guild.get_channel(voice_channel.id)
                if not refreshed_channel or not isinstance(refreshed_channel, discord.VoiceChannel):
                     logging.warning(f"Target channel {voice_channel.name} ({voice_channel.id}) no longer exists. Skipping TTS.")
                     continue
                if not any(m for m in refreshed_channel.members if not m.bot):
                     logging.info(f"Target channel {refreshed_channel.name} is empty (or only contains bots). Skipping TTS: '{message[:30]}...'")
                     # Check if bot should leave
                     await self.check_and_disconnect_if_alone(guild_id, refreshed_channel)
                     continue

                # 1. Check/Generate TTS file
                if not os.path.exists(tts_path):
                    logging.debug(f"TTS file {tts_path} not found in cache. Generating...")
                    success = await self.generate_tts_async(message, tts_path)
                    if not success:
                        logging.error(f"Skipping TTS for '{message[:30]}...' due to generation failure.")
                        continue # Skip to next item in queue
                else:
                     logging.debug(f"Using cached TTS file: {tts_path}")

                # 2. Get Voice Client (Connect if necessary)
                vc = await self._get_or_connect_vc(guild, refreshed_channel)
                if not vc:
                    logging.error(f"Failed to get voice client for guild {guild_id}, channel {refreshed_channel.id}. Skipping TTS.")
                    # Could potentially re-queue the message, but might lead to loops
                    continue

                # 3. Play TTS
                if vc.is_playing() or vc.is_paused():
                    logging.warning(f"Voice client in guild {guild_id} is already playing/paused. Waiting might be needed, but currently skipping: '{message[:30]}...'")
                    # Re-queue the message at the front for next iteration?
                    # guild_tts_queues[guild_id].appendleft((voice_channel, message, tts_path))
                    # await asyncio.sleep(0.5) # Small delay before next attempt
                    # Let's skip for now to avoid complex queue/state management
                    continue

                logging.info(f"Playing TTS in G:{guild_id} C:{refreshed_channel.id}: {tts_path}")
                # Use lock to prevent concurrent play attempts on the same VC
                async with voice_connection_lock:
                    if not vc.is_connected(): # Check connection again inside lock
                        logging.warning(f"VC for G:{guild_id} disconnected before playback could start. Skipping.")
                        continue
                    
                    playback_finished = asyncio.Event()
                    def after_play(error):
                        if error:
                            logging.error(f"Error during TTS playback in guild {guild_id}: {error}")
                        else:
                            logging.debug(f"Finished playing TTS in guild {guild_id}: {tts_path}")
                        # Signal that playback is finished, regardless of error
                        asyncio.get_event_loop().call_soon_threadsafe(playback_finished.set)
                        # Check if bot should disconnect *after* playing
                        # Need to run this check in the main event loop
                        asyncio.run_coroutine_threadsafe(self.check_and_disconnect_if_alone(guild_id, refreshed_channel), self.bot.loop)

                    try:
                        audio_source = discord.FFmpegPCMAudio(tts_path, executable=FFMPEG_EXECUTABLE)
                        vc.play(audio_source, after=after_play)

                        # Wait for playback to finish (with a timeout)
                        try:
                             await asyncio.wait_for(playback_finished.wait(), timeout=30.0) # Timeout after 30s
                        except asyncio.TimeoutError:
                             logging.error(f"Timeout waiting for TTS playback to finish in guild {guild_id}. Stopping playback.")
                             if vc.is_playing():
                                 vc.stop()
                             # Run check anyway, might need to disconnect
                             await self.check_and_disconnect_if_alone(guild_id, refreshed_channel)

                    except discord.ClientException as e:
                        logging.error(f"Discord ClientException during TTS playback in guild {guild_id}: {e}. Skipping.")
                        if vc.is_playing(): vc.stop()
                        await self.check_and_disconnect_if_alone(guild_id, refreshed_channel)
                    except Exception as e:
                         logging.error(f"Unexpected error during TTS playback setup/wait in guild {guild_id}: {e}", exc_info=True)
                         if vc.is_playing(): vc.stop()
                         await self.check_and_disconnect_if_alone(guild_id, refreshed_channel)

            except asyncio.CancelledError:
                 logging.info(f"TTS queue processing for guild {guild_id} cancelled.")
                 break # Exit loop if task is cancelled
            except Exception as e:
                 logging.error(f"Unexpected error in TTS queue processing loop for guild {guild_id}: {e}", exc_info=True)
                 # Avoid tight loop errors, add a small delay
                 await asyncio.sleep(1)

        logging.info(f"TTS queue processing finished for guild {guild_id}.")
        guild_tts_tasks.pop(guild_id, None) # Remove task entry when queue is empty
        # Final check for disconnection if queue ended and bot might be alone
        last_vc = guild_voice_clients.get(guild_id)
        if last_vc and last_vc.is_connected():
             await self.check_and_disconnect_if_alone(guild_id, last_vc.channel)


    async def _get_or_connect_vc(self, guild: discord.Guild, channel: discord.VoiceChannel) -> discord.VoiceClient | None:
        """Gets the existing voice client for the guild or connects to the specified channel."""
        guild_id = guild.id
        vc = guild_voice_clients.get(guild_id)

        async with voice_connection_lock:
            # Check if already connected to the correct channel
            if vc and vc.is_connected() and vc.channel == channel:
                logging.debug(f"Using existing voice client for G:{guild_id} C:{channel.id}")
                return vc

            # Check if connected to a different channel
            if vc and vc.is_connected() and vc.channel != channel:
                logging.info(f"Moving voice client in G:{guild_id} from {vc.channel.name} to {channel.name}")
                try:
                    await vc.move_to(channel)
                    guild_voice_clients[guild_id] = vc # Update entry just in case
                    return vc
                except asyncio.TimeoutError:
                    logging.error(f"Timeout moving voice client in G:{guild_id}. Attempting disconnect and reconnect.")
                    await self._safe_disconnect(vc)
                    # Fall through to reconnect logic
                except Exception as e:
                     logging.error(f"Error moving voice client in G:{guild_id}: {e}. Attempting disconnect and reconnect.", exc_info=True)
                     await self._safe_disconnect(vc)
                     # Fall through to reconnect logic

            # If not connected or previous connection failed, connect anew
            logging.info(f"Connecting to voice channel {channel.name} ({channel.id}) in guild {guild_id}")
            if guild_id in reconnecting_guilds:
                 logging.warning(f"Connection attempt skipped for guild {guild_id}, already reconnecting.")
                 return None
            
            reconnecting_guilds.add(guild_id)
            try:
                 # Check permissions *before* attempting connect
                 if not has_required_permissions(channel):
                      logging.error(f"Cannot connect: Missing permissions for G:{guild_id} C:{channel.id}")
                      return None
                 new_vc = await channel.connect(timeout=30.0, reconnect=True)
                 guild_voice_clients[guild_id] = new_vc
                 logging.info(f"Successfully connected to {channel.name} ({channel.id}) in guild {guild_id}")
                 return new_vc
            except asyncio.TimeoutError:
                 logging.error(f"Timeout connecting to {channel.name} ({channel.id}) in guild {guild_id}.")
                 return None
            except discord.ClientException as e:
                 # Handles cases like "Already connected to a voice channel..."
                 logging.error(f"ClientException connecting to {channel.name} ({channel.id}) in G:{guild_id}: {e}")
                 # Check if we somehow have a client state mismatch
                 current_vc = discord.utils.get(self.bot.voice_clients, guild=guild)
                 if current_vc:
                     logging.warning(f"ClientException, but found existing VC for G:{guild_id} in channel {current_vc.channel.id}. Using it.")
                     guild_voice_clients[guild_id] = current_vc # Correct internal state
                     # Need to check if it's in the right channel now...
                     if current_vc.channel != channel:
                          logging.info(f"Existing VC is in wrong channel ({current_vc.channel.id}), attempting move to {channel.id}")
                          try:
                              await current_vc.move_to(channel)
                              return current_vc
                          except Exception as move_e:
                               logging.error(f"Failed to move VC after ClientException: {move_e}. Disconnecting.")
                               await self._safe_disconnect(current_vc)
                               return None
                     else:
                          return current_vc # Was already in the right channel somehow
                 return None
            except Exception as e:
                 logging.error(f"Failed to connect to {channel.name} ({channel.id}) in guild {guild_id}: {e}", exc_info=True)
                 return None
            finally:
                 reconnecting_guilds.discard(guild_id)

    async def check_and_disconnect_if_alone(self, guild_id: int, channel: discord.VoiceChannel):
        """Checks if the bot is the only member left in the channel and disconnects if so."""
        # Add a small delay to allow state updates to propagate
        await asyncio.sleep(1.0) 

        vc = guild_voice_clients.get(guild_id)
        # Ensure we're checking the correct channel and the bot is connected
        if not vc or not vc.is_connected() or vc.channel != channel:
            # logging.debug(f"Skipping disconnect check: VC not found, not connected, or in different channel ({vc.channel.id if vc else 'N/A'} vs {channel.id})")
            return

        # Refresh channel members
        refreshed_channel = self.bot.get_channel(channel.id)
        if not refreshed_channel or not isinstance(refreshed_channel, discord.VoiceChannel):
            logging.warning(f"Channel {channel.id} not found during disconnect check.")
            # If channel doesn't exist, we should disconnect
            await self._safe_disconnect(vc)
            return

        # Check if only the bot is left
        if len(refreshed_channel.members) == 1 and refreshed_channel.members[0] == guild.me:
            logging.info(f"Bot is alone in voice channel {refreshed_channel.name} ({refreshed_channel.id}). Disconnecting.")
            await self._safe_disconnect(vc)
        # else:
        #      logging.debug(f"Bot is not alone in {refreshed_channel.name}. Members: {[m.name for m in refreshed_channel.members]}")

    @tasks.loop(hours=1)
    async def cleanup_tts_files(self):
        """Periodically cleans up old TTS files from the cache directory."""
        logging.info("Starting TTS cache cleanup task...")
        now = time.time()
        cutoff = now - (24 * 3600) # Delete files older than 24 hours
        count = 0
        try:
            for filename in os.listdir(TTS_CACHE_DIR):
                if filename.endswith(".mp3"):
                    file_path = os.path.join(TTS_CACHE_DIR, filename)
                    try:
                        if os.path.getmtime(file_path) < cutoff:
                            os.remove(file_path)
                            logging.debug(f"Deleted old TTS cache file: {filename}")
                            count += 1
                    except FileNotFoundError:
                        continue # File already deleted
                    except OSError as e:
                        logging.error(f"Error deleting TTS cache file {file_path}: {e}")
            logging.info(f"TTS cache cleanup finished. Deleted {count} old files.")
        except Exception as e:
             logging.error(f"Error during TTS cache cleanup task: {e}", exc_info=True)

    @cleanup_tts_files.before_loop
    async def before_cleanup_tts_files(self):
        await self.bot.wait_until_ready()
        logging.info("TTS file cleanup loop waiting for bot readiness... Done.")

    @tasks.loop(minutes=2)
    async def check_voice_connections(self):
        """Periodically checks if voice connections are still valid and attempts reconnection/cleanup."""
        logging.debug("Running periodic voice connection check...")
        # Check tracked clients
        guild_ids_to_remove = []
        for guild_id, vc in list(guild_voice_clients.items()): # Iterate over copy
            guild = self.bot.get_guild(guild_id)
            if not guild:
                 logging.warning(f"Guild {guild_id} not found during connection check. Removing VC state.")
                 await self._safe_disconnect(vc)
                 guild_ids_to_remove.append(guild_id)
                 continue
            
            if not vc.is_connected():
                logging.warning(f"Tracked VC for guild {guild_id} is not connected. Cleaning up state.")
                # Don't call disconnect here, just clean up internal state
                guild_ids_to_remove.append(guild_id)
                guild_tts_queues.pop(guild_id, None)
                if guild_id in guild_tts_tasks:
                    guild_tts_tasks[guild_id].cancel()
                    guild_tts_tasks.pop(guild_id, None)
                reconnecting_guilds.discard(guild_id)
                continue
            
            # Check if channel still exists and bot is alone
            channel = guild.get_channel(vc.channel.id)
            if not channel or not isinstance(channel, discord.VoiceChannel):
                 logging.warning(f"VC for guild {guild_id} is connected to a non-existent channel {vc.channel.id}. Disconnecting.")
                 await self._safe_disconnect(vc)
                 guild_ids_to_remove.append(guild_id)
            elif len(channel.members) == 1 and channel.members[0] == guild.me:
                 logging.info(f"VC for guild {guild_id} found bot alone in {channel.name}. Disconnecting.")
                 await self._safe_disconnect(vc)
                 guild_ids_to_remove.append(guild_id)
            # Optional: Check latency? vc.latency

        for gid in guild_ids_to_remove:
             guild_voice_clients.pop(gid, None)

        # Check actual bot VCs vs tracked VCs (consistency check)
        actual_vc_guild_ids = {vc.guild.id for vc in self.bot.voice_clients}
        tracked_vc_guild_ids = set(guild_voice_clients.keys())

        # VCs the bot has but aren't tracked internally
        untracked_vcs = actual_vc_guild_ids - tracked_vc_guild_ids
        for guild_id in untracked_vcs:
             vc = discord.utils.get(self.bot.voice_clients, guild__id=guild_id)
             if vc:
                 logging.warning(f"Found untracked voice client for guild {guild_id} in channel {vc.channel.id}. Adding to internal tracking.")
                 guild_voice_clients[guild_id] = vc
                 # Ensure TTS task isn't running erroneously if queue exists?
                 if guild_id in guild_tts_queues and guild_tts_queues[guild_id] and (guild_id not in guild_tts_tasks or guild_tts_tasks[guild_id].done()):
                     logging.warning(f"Starting potentially orphaned TTS task for guild {guild_id} found during consistency check.")
                     guild_tts_tasks[guild_id] = asyncio.create_task(self.process_guild_tts_queue(guild_id))
                     guild_tts_tasks[guild_id].add_done_callback(self._handle_tts_task_completion)

        # VCs tracked internally but the bot doesn't actually have (already handled by the first loop)
        # stale_tracked_vcs = tracked_vc_guild_ids - actual_vc_guild_ids
        # for guild_id in stale_tracked_vcs:
        #      logging.warning(f"Found stale tracked voice client for guild {guild_id}. Removing internal state.")
        #      guild_voice_clients.pop(guild_id, None)
        #      guild_tts_queues.pop(guild_id, None)
        #      if guild_id in guild_tts_tasks:
        #          guild_tts_tasks[guild_id].cancel()
        #          guild_tts_tasks.pop(guild_id, None)
        #      reconnecting_guilds.discard(guild_id)

        logging.debug("Periodic voice connection check finished.")


    @check_voice_connections.before_loop
    async def before_check_voice_connections(self):
        await self.bot.wait_until_ready()
        logging.info("Voice connection check loop waiting for bot readiness... Done.")


    # --- Commands --- 

    # Example command (can be removed or adapted)
    @commands.command(name='say')
    @commands.has_permissions(manage_messages=True) # Example permission
    async def say_tts(self, ctx: commands.Context, *, text: str):
        """(Admin) Makes the bot say the given text in your current voice channel."""
        if not ctx.author.voice or not ctx.author.voice.channel:
            await ctx.send("You need to be in a voice channel to use this command.", delete_after=10)
            return

        voice_channel = ctx.author.voice.channel
        if voice_channel.id in EXCLUDED_VOICE_CHANNEL_IDS:
            await ctx.send("TTS is disabled for this voice channel.", delete_after=10)
            return
            
        if not has_required_permissions(voice_channel):
             await ctx.send("I don't have permission to join or speak in your voice channel.", delete_after=10)
             return

        logging.info(f"Admin command !say used by {ctx.author} in G:{ctx.guild.id} C:{voice_channel.id}. Text: {text}")
        await self.queue_tts(ctx.guild, voice_channel, text)
        await ctx.message.add_reaction('🔊')

    @commands.command()
    @commands.has_permissions(manage_guild=True) # Example permission
    async def leave_voice(self, ctx: commands.Context):
        """(Admin) Forces the bot to leave its current voice channel in this guild."""
        vc = guild_voice_clients.get(ctx.guild.id)
        if vc and vc.is_connected():
             await ctx.send(f"Leaving voice channel {vc.channel.name}...")
             await self._safe_disconnect(vc)
             await ctx.message.add_reaction('✅')
        else:
             await ctx.send("I am not currently in a voice channel in this server.", delete_after=10)

async def setup(bot: commands.Bot):
    await bot.add_cog(TTSCog(bot)) 