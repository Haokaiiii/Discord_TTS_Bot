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
        self.tts_queue = defaultdict(lambda: asyncio.Queue(maxsize=10)) # {guild_id: Queue[Tuple[str, discord.VoiceChannel, discord.Member]]}
        self.tts_processor_tasks = {} # {guild_id: asyncio.Task}
        self.guild_locks = defaultdict(asyncio.Lock) # {guild_id: Lock}
        self.debounce_timers = defaultdict(dict) # {guild_id: {member_id: asyncio.TimerHandle}}
        self.loop = asyncio.get_event_loop() # Store the main event loop
        # User ID for special TTS message (Replace if needed)
        self.special_user_id = 647282381841104897 

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
        """Safely disconnects a voice client, handling potential errors and timeout."""
        if vc and vc.is_connected():
            guild_id = vc.guild.id
            logging.info(f"Safely disconnecting from voice channel {vc.channel.id} in guild {guild_id}")
            try:
                async with voice_connection_lock:
                    if vc.is_playing():
                        vc.stop()
                    # Add timeout to the disconnect call itself
                    try:
                        await asyncio.wait_for(vc.disconnect(force=True), timeout=5.0) 
                        logging.info(f"Successfully disconnected from voice in guild {guild_id}")
                    except asyncio.TimeoutError:
                        logging.error(f"Timeout waiting for disconnect confirmation from Discord for guild {guild_id}. Proceeding with cleanup.")
                        # Force internal state update even if Discord didn't confirm
                    except Exception as inner_e:
                        logging.error(f"Error during vc.disconnect call in guild {guild_id}: {inner_e}", exc_info=True)

            except Exception as e:
                # Catch errors acquiring lock or other unexpected issues
                logging.error(f"Error during outer safe disconnect logic in guild {guild_id}: {e}", exc_info=True)
            finally:
                # Cleanup internal state regardless of disconnect success/timeout
                guild_voice_clients.pop(guild_id, None)
                # Reset relevant states (consider if queue/task cleanup needed here)
                # guild_tts_queues.pop(guild_id, None)
                # if guild_id in self.tts_processor_tasks:
                #     self.tts_processor_tasks[guild_id].cancel()
                #     self.tts_processor_tasks.pop(guild_id, None)
                reconnecting_guilds.discard(guild_id)
        # else:
             # logging.debug(f"_safe_disconnect called but VC not connected or invalid.")

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        if member.bot:
            return

        guild = member.guild
        guild_id = guild.id
        member_id = member.id

        before_channel = before.channel
        after_channel = after.channel
        is_before_excluded = before_channel and before_channel.id in EXCLUDED_VOICE_CHANNEL_IDS
        is_after_excluded = after_channel and after_channel.id in EXCLUDED_VOICE_CHANNEL_IDS

        action_type = None
        target_channel_for_debounce = None

        # Determine action type
        if (not before_channel or is_before_excluded) and (after_channel and not is_after_excluded):
            action_type = 'join'
            target_channel_for_debounce = after_channel
        elif (before_channel and not is_before_excluded) and (not after_channel or is_after_excluded):
            action_type = 'leave'
            target_channel_for_debounce = before_channel # Announce in the channel they left
        elif (before_channel and not is_before_excluded) and \
             (after_channel and not is_after_excluded) and \
             before_channel != after_channel:
            action_type = 'switch'
            target_channel_for_debounce = after_channel # Announce in the destination channel

        # If a relevant action occurred
        if action_type and target_channel_for_debounce:
            # --- Debounce Logic --- 
            timer_key = (guild_id, member_id)
            
            # If a timer already exists for this user, cancel it
            if timer_key in self.debounce_timers:
                self.debounce_timers[timer_key].cancel()
                logging.debug(f"Cancelled existing debounce timer for {member.display_name} ({member_id}) in guild {guild_id}")
                # No need to remove here, call_later replaces

            # Start a new debounce timer
            logging.info(f"Detected {action_type} for {member.display_name} ({member_id}). Starting debounce timer ({DEBOUNCE_TIME}s) for channel {target_channel_for_debounce.id}.")
            self.debounce_timers[timer_key] = self.loop.call_later(
                DEBOUNCE_TIME,
                lambda: asyncio.create_task( # Use lambda to avoid immediate coro creation
                    self._debounce_tts_queue(member, before, after, action_type, target_channel_for_debounce)
                )
            )

        # --- Disconnect Check --- (Independent of debounce)
        # Check if the bot should disconnect from the 'before' channel if it was valid and might now be empty
        if before_channel and not is_before_excluded and action_type in ['leave', 'switch']:
             await self.check_and_disconnect_if_alone(guild_id, before_channel, guild)
        
        # NOTE: Removed the immediate queue_tts calls from here.
        # All TTS queuing is now handled by _debounce_tts_queue.

    async def _debounce_tts_queue(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState, action_type: str, target_channel: discord.VoiceChannel):
        """Called after debounce timer; generates text and queues TTS."""
        guild_id = member.guild.id
        member_id = member.id
        member_name = get_preferred_name(member)
        timer_key = (guild_id, member_id)

        # Clean up the finished timer handle reference
        self.debounce_timers.pop(timer_key, None)

        # Re-check member state *after* delay 
        current_voice_state = member.voice
        expected_channel_id = target_channel.id
        current_channel_id = current_voice_state.channel.id if current_voice_state and current_voice_state.channel else None

        # --- More detailed logging for skipped announcements --- 
        skip_reason = None
        if action_type == 'leave':
            if current_channel_id and current_channel_id not in EXCLUDED_VOICE_CHANNEL_IDS:
                 skip_reason = f"User {member_name} rejoined/moved to channel {current_channel_id} during leave debounce."
            elif not has_required_permissions(target_channel):
                 skip_reason = f"Missing permissions in original channel {target_channel.id} to announce leave."
        elif action_type in ['join', 'switch']:
             if current_channel_id != expected_channel_id:
                 skip_reason = f"User {member_name} not in expected channel {expected_channel_id} after debounce (current: {current_channel_id})."
             elif not has_required_permissions(target_channel):
                 skip_reason = f"Missing permissions in destination channel {target_channel.id} to announce {action_type}."
        
        if skip_reason:
            logging.info(f"Aborting {action_type} announcement for {member_name}: {skip_reason}")
            return
        # --- End logging --- 

        # Generate TTS text based on action and user
        tts_text = ""
        if action_type == 'join':
            if member_id == self.special_user_id:
                tts_text = f"欢迎我的主人{member_name}, muamuamua"
            else:
                tts_text = f"欢迎 {member_name}"
        elif action_type == 'leave':
             tts_text = f"{member_name} 滚了"
        elif action_type == 'switch':
             tts_text = f"{member_name} 叛变了"

        if tts_text:
            logging.info(f"Debounce finished. Queueing TTS for {action_type}: {tts_text} in G:{guild_id} C:{target_channel.id}")
            await self.queue_tts(member.guild, target_channel, tts_text)
        else:
            # This case should ideally not happen if skip logic is correct
            logging.warning(f"Debounce finished for {action_type} by {member_name}. No TTS text generated despite passing checks.")

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
        """Adds a TTS message to the guild's asyncio Queue and ensures the processing task is running."""
        guild_id = guild.id
        if not message:
            return

        # Ensure cache directory exists
        os.makedirs(TTS_CACHE_DIR, exist_ok=True)

        # Generate TTS file path (hash message for caching)
        text_hash = hashlib.md5(message.encode('utf-8')).hexdigest()
        output_filename = f"{text_hash}.mp3"
        output_path = os.path.join(TTS_CACHE_DIR, output_filename)

        # Add to the COG'S asyncio.Queue
        try:
            # Package necessary context (guild needed for disconnect check later)
            await self.tts_queue[guild_id].put((voice_channel, message, guild, output_path))
            logging.debug(f"Added to asyncio TTS queue for guild {guild_id}: '{message[:30]}...' Target: {voice_channel.name}")
        except asyncio.QueueFull:
            logging.warning(f"TTS queue full for guild {guild_id}. Skipping message: '{message[:30]}...'")
            return # Skip if queue is full

        # Ensure the processing task for this guild is running using self.tts_processor_tasks
        if guild_id not in self.tts_processor_tasks or self.tts_processor_tasks[guild_id].done():
            logging.info(f"Starting TTS processing task for guild {guild_id} using asyncio.Queue.")
            self.tts_processor_tasks[guild_id] = asyncio.create_task(self.process_guild_tts_queue(guild_id))
            self.tts_processor_tasks[guild_id].add_done_callback(self._handle_tts_task_completion)

    def _handle_tts_task_completion(self, task: asyncio.Task):
        # Use self.tts_processor_tasks
        guild_id = None
        for gid, t in list(self.tts_processor_tasks.items()): # Iterate copy
            if t == task:
                guild_id = gid
                del self.tts_processor_tasks[gid] # Remove completed/failed task
                break
        try:
            task.result() # Raise exception if one occurred
            logging.info(f"TTS processing task for guild {guild_id or 'Unknown'} completed normally.")
        except asyncio.CancelledError:
            logging.info(f"TTS processing task for guild {guild_id or 'Unknown'} cancelled.")
        except Exception as e:
            logging.error(f"TTS processing task for guild {guild_id or 'Unknown'} failed: {e}", exc_info=True)

    async def generate_tts_async(self, text: str) -> str | None:
        """Generates TTS audio file using gTTS in a ThreadPoolExecutor, returns path on success."""
        loop = asyncio.get_event_loop()
        # Generate path within this function
        text_hash = hashlib.md5(text.encode('utf-8')).hexdigest()
        output_filename = f"{text_hash}.mp3"
        output_path = os.path.join(TTS_CACHE_DIR, output_filename)

        # Check cache first
        if os.path.exists(output_path):
             logging.debug(f"Using cached TTS file: {output_path}")
             return output_path

        logging.debug(f"Generating TTS file: {output_path}")
        try:
            await loop.run_in_executor(
                tts_executor,
                self._blocking_gtts_call, # Run the blocking function
                text,
                output_path
            )
            logging.info(f"Successfully generated TTS file: {output_path}")
            return output_path
        except Exception as e:
            logging.error(f"Failed to generate TTS for '{text[:30]}...': {e}", exc_info=True)
            try: # Attempt cleanup
                 if os.path.exists(output_path): os.remove(output_path)
            except OSError as oe: logging.error(f"Error removing failed TTS file {output_path}: {oe}")
            return None # Indicate failure

    def _blocking_gtts_call(self, text: str, output_path: str):
        """The actual blocking gTTS call. DO NOT run this directly in the main event loop."""
        tts = gTTS(text=text, lang='zh-CN')
        tts.save(output_path)

    async def process_guild_tts_queue(self, guild_id: int):
        """Processes the TTS asyncio.Queue for a specific guild."""
        guild = self.bot.get_guild(guild_id)
        if not guild:
            logging.error(f"Cannot process TTS queue: Guild {guild_id} not found.")
            # Consider clearing self.tts_queue[guild_id] here if necessary
            return
        
        logging.info(f"TTS queue processor started for guild {guild_id}.")

        while True:
            try:
                # --- CRITICAL FIX: Use self.tts_queue (asyncio.Queue) --- 
                logging.debug(f"G:{guild_id} Waiting for item from asyncio.Queue...")
                # Wait indefinitely until an item is available or task is cancelled
                # Ensure item structure matches what queue_tts puts
                target_channel, tts_text, guild_context, tts_path = await self.tts_queue[guild_id].get() 
                logging.debug(f"G:{guild_id} Got item: '{tts_text[:30]}...' for C:{target_channel.id}")
                # Use the guild object received from the queue for consistency
                guild = guild_context 
                # --- END CRITICAL FIX --- 

                # Refresh channel state and check members
                refreshed_channel = self.bot.get_channel(target_channel.id)
                if not refreshed_channel or not isinstance(refreshed_channel, discord.VoiceChannel):
                    logging.warning(f"G:{guild_id} Target channel {target_channel.id} no longer exists/invalid. Skipping.")
                    self.tts_queue[guild_id].task_done() # Mark item as processed
                    continue

                human_members = [m for m in refreshed_channel.members if not m.bot]
                vc = guild.voice_client # Get current VC for this guild

                # Check if channel empty *before* connecting/playing
                if not human_members and (not vc or vc.channel != refreshed_channel):
                    logging.info(f"G:{guild_id} C:{refreshed_channel.id} is empty. Skipping TTS: '{tts_text[:30]}...'")
                    self.tts_queue[guild_id].task_done()
                    continue 
                elif not human_members and vc and vc.channel == refreshed_channel:
                     logging.info(f"G:{guild_id} C:{refreshed_channel.id} only contains bot. Skipping TTS: '{tts_text[:30]}...'. Disconnecting.")
                     await self._safe_disconnect(vc)
                     self.tts_queue[guild_id].task_done()
                     continue 

                # Check/Generate TTS file (Path was already determined by queue_tts)
                if not os.path.exists(tts_path):
                     logging.debug(f"G:{guild_id} Generating TTS file (not found in cache): {tts_path}")
                     # This call generates the file if needed, returns None on failure
                     generated_path = await self.generate_tts_async(tts_text) 
                     if not generated_path:
                         logging.error(f"G:{guild_id} Failed to generate TTS file for: {tts_text[:30]}... Skipping.")
                         self.tts_queue[guild_id].task_done()
                         continue
                     # Should normally be the same path, but use returned path just in case
                     tts_path = generated_path 
                else:
                    logging.debug(f"G:{guild_id} Using cached TTS file: {tts_path}")

                # Get or connect voice client
                vc = await self._get_or_connect_vc(guild, refreshed_channel) # Pass guild object
                if not vc:
                    logging.error(f"G:{guild_id} Failed to get voice client for C:{refreshed_channel.id}. Skipping TTS.")
                    self.tts_queue[guild_id].task_done()
                    continue

                # Playback logic 
                logging.info(f"Playing TTS in G:{guild_id} C:{refreshed_channel.id}: {tts_path}")
                async with voice_connection_lock:
                    # Re-check connection status after acquiring lock
                    if not vc.is_connected():
                        logging.warning(f"G:{guild_id} VC disconnected before playback locked. Skipping.")
                        self.tts_queue[guild_id].task_done()
                        continue
                    
                    # Check if already playing (should be prevented by lock, but belts and suspenders)
                    if vc.is_playing():
                         logging.warning(f"G:{guild_id} VC is already playing. Skipping TTS: '{tts_text[:30]}...'")
                         self.tts_queue[guild_id].task_done() # Skip this item
                         continue

                    playback_finished = asyncio.Event()
                    
                    def after_play(error):
                        if error: logging.error(f"G:{guild_id} Playback error: {error}")
                        else: logging.debug(f"G:{guild_id} Playback finished: {tts_path}")
                        self.loop.call_soon_threadsafe(playback_finished.set)
                        asyncio.run_coroutine_threadsafe(self.check_and_disconnect_if_alone(guild_id, refreshed_channel, guild), self.loop)

                    try:
                        audio_source = discord.FFmpegPCMAudio(tts_path, executable=FFMPEG_EXECUTABLE)
                        vc.play(audio_source, after=after_play)
                        try:
                             await asyncio.wait_for(playback_finished.wait(), timeout=45.0)
                        except asyncio.TimeoutError:
                             logging.error(f"G:{guild_id} Timeout waiting for playback finish. Stopping.")
                             if vc.is_playing(): vc.stop()
                             # check_and_disconnect is called by after_play even on timeout stop
                        # Clean up file only after successful play/stop
                        try: 
                            if os.path.exists(tts_path):
                                 logging.debug(f"G:{guild_id} Removing TTS file: {tts_path}")
                                 os.remove(tts_path)
                        except OSError as e: logging.warning(f"G:{guild_id} Could not remove TTS file {tts_path}: {e}")

                    except discord.ClientException as e: 
                         logging.error(f"G:{guild_id} ClientException during playback: {e}. Checking disconnect.")
                         if vc.is_playing(): vc.stop()
                         # Don't run check_and_disconnect here, let after_play handle it if called
                    except Exception as e:
                         logging.error(f"G:{guild_id} Unexpected error during playback: {e}", exc_info=True)
                         if vc.is_playing(): vc.stop()
                         # Don't run check_and_disconnect here, let after_play handle it if called

                # Mark item as processed *after* lock released
                self.tts_queue[guild_id].task_done()
                logging.debug(f"G:{guild_id} Marked task done for: '{tts_text[:30]}...'")

            except asyncio.CancelledError:
                 logging.info(f"TTS processing task for guild {guild_id} cancelled.")
                 break # Exit loop on cancellation
            except Exception as e:
                 # Error likely occurred getting item from queue or early checks
                 logging.error(f"Unexpected error in TTS queue processing loop (before playback) for guild {guild_id}: {e}", exc_info=True)
                 # If an error occurs getting item, we might not have an item to mark done.
                 # Check if queue still exists and potentially mark done if error happened after get()
                 if guild_id in self.tts_queue and 'target_channel' in locals(): # Check if get() succeeded
                      try: 
                           self.tts_queue[guild_id].task_done()
                           logging.warning(f"G:{guild_id} Marked task done after pre-playback error.")
                      except ValueError: pass 
                 await asyncio.sleep(1) # Wait briefly before next attempt

        # ... (End of task cleanup) ...
        logging.info(f"TTS processing task for guild {guild_id} has stopped.")
        # Ensure task reference is removed
        self.tts_processor_tasks.pop(guild_id, None)

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

    async def check_and_disconnect_if_alone(self, guild_id: int, channel: discord.VoiceChannel | None, guild: discord.Guild):
        """Checks if the bot is alone in the channel and disconnects if true."""
        if not channel: 
            # logging.debug("check_and_disconnect: No channel provided.")
            return

        vc = guild.voice_client
        # Check if bot is connected to THIS specific channel
        if not vc or not vc.is_connected() or vc.channel.id != channel.id:
            # logging.debug(f"check_and_disconnect: Bot not connected or not in target channel {channel.id}")
            return

        # Refresh channel state immediately before checking members
        refreshed_channel = self.bot.get_channel(channel.id)
        if not refreshed_channel or not isinstance(refreshed_channel, discord.VoiceChannel):
            logging.warning(f"Channel {channel.id} not found during disconnect check. Attempting disconnect anyway.")
            await self._safe_disconnect(vc)
            return

        # Check members *in the refreshed channel state*
        human_members = [m for m in refreshed_channel.members if not m.bot]

        if not human_members:
            # --- Optimization: Remove grace period --- 
            logging.info(f"Bot is alone in {refreshed_channel.name} ({refreshed_channel.id}). Disconnecting immediately.")
            await self._safe_disconnect(vc)
            # --- End Optimization --- 
        # else:
            # logging.debug(f"Bot not alone in {refreshed_channel.name}. Members: {[m.name for m in human_members]}")

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