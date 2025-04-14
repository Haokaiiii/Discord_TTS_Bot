import discord
from discord.ext import commands, tasks
import logging
import asyncio
from datetime import datetime, time, timedelta
from collections import defaultdict
from pytz import timezone

from utils.database import DatabaseManager
from utils.helpers import get_preferred_name, check_channel, send_to_command_channel
from utils.plotting import generate_periodic_chart, generate_co_occurrence_heatmap, generate_relationship_network_graph
from utils.config import EXCLUDED_VOICE_CHANNEL_IDS

class StatsCog(commands.Cog):
    def __init__(self, bot: commands.Bot, db_manager: DatabaseManager):
        self.bot = bot
        self.db_manager = db_manager
        # In-memory data structures
        self.voice_activity = defaultdict(dict)  # {guild_id: {member_id: join_time}}
        self.channel_users = defaultdict(lambda: defaultdict(dict)) # {guild_id: {channel_id: {member_id: join_time}}}
        # Loaded from DB on startup
        self.voice_stats = self.db_manager.load_voice_stats() # {guild_id: {member_id: {period: seconds}}} nested dict
        self.co_occurrence_stats = self.db_manager.load_co_occurrence_stats() # {guild_id: {(m1, m2): seconds}}

        self.save_stats.start()
        self.schedule_reports()
        logging.info("StatsCog initialized.")

    def cog_unload(self):
        self.save_stats.cancel()
        # Persist any final stats before shutdown? Maybe call save_stats sync?
        logging.info("StatsCog unloaded.")

    def _initialize_guild_stats(self, guild_id):
        """Initializes data structures for a new guild."""
        if guild_id not in self.voice_stats:
            self.voice_stats[guild_id] = defaultdict(lambda: defaultdict(float))
            logging.info(f"Initialized voice_stats for guild {guild_id}")
        if guild_id not in self.co_occurrence_stats:
            self.co_occurrence_stats[guild_id] = defaultdict(float)
            logging.info(f"Initialized co_occurrence_stats for guild {guild_id}")
        if guild_id not in self.voice_activity:
             self.voice_activity[guild_id] = {}
             logging.info(f"Initialized voice_activity for guild {guild_id}")
        if guild_id not in self.channel_users:
            self.channel_users[guild_id] = defaultdict(dict)
            logging.info(f"Initialized channel_users for guild {guild_id}")


    def _update_stats(self, guild_id, member_id, duration_seconds):
        """Updates voice duration stats for a member across all periods."""
        self._initialize_guild_stats(guild_id) # Ensure guild exists
        if member_id not in self.voice_stats[guild_id]:
            self.voice_stats[guild_id][member_id] = defaultdict(float)

        stats = self.voice_stats[guild_id][member_id]
        stats['total'] += duration_seconds
        stats['daily'] += duration_seconds
        stats['weekly'] += duration_seconds
        stats['monthly'] += duration_seconds
        stats['yearly'] += duration_seconds
        # logging.debug(f"Updated stats for member {member_id} in guild {guild_id}: {duration_seconds}s. New totals: {stats}")

    def _update_co_occurrence(self, guild_id, channel_id, member_id, action):
        """Updates co-occurrence stats when a member joins or leaves a channel."""
        self._initialize_guild_stats(guild_id)
        now = datetime.now()

        current_users_in_channel = list(self.channel_users[guild_id].get(channel_id, {}).keys())
        join_time = self.channel_users[guild_id].get(channel_id, {}).get(member_id)

        if action == 'join':
            if member_id in self.channel_users[guild_id].get(channel_id, {}):
                logging.warning(f"Member {member_id} already marked as joined channel {channel_id} in guild {guild_id}. Ignoring duplicate join.")
                return # Already processed?

            # Update join time for the joining member
            self.channel_users[guild_id][channel_id][member_id] = now
            logging.debug(f"Member {member_id} recorded joining channel {channel_id} at {now}. Current users: {current_users_in_channel}")

            # Add co-occurrence duration for the time others were in the channel before this member joined
            for other_member_id in current_users_in_channel:
                if other_member_id == member_id: continue # Should not happen if logic is correct
                other_join_time = self.channel_users[guild_id][channel_id].get(other_member_id)
                if other_join_time:
                    pair = tuple(sorted((member_id, other_member_id)))
                    # No duration to add here, just record the start of overlap
                    logging.debug(f"Overlap started between {member_id} and {other_member_id} in channel {channel_id}.")

        elif action == 'leave':
            if member_id not in self.channel_users[guild_id].get(channel_id, {}):
                # This can happen if the bot started while the user was already in the channel
                # Or if there was a state mismatch. We can't calculate duration reliably here.
                logging.warning(f"Member {member_id} left channel {channel_id} but was not tracked. Cannot update co-occurrence.")
                if channel_id in self.channel_users[guild_id] and member_id in self.channel_users[guild_id][channel_id]:
                     del self.channel_users[guild_id][channel_id][member_id] # Clean up state if present
                return

            if join_time is None:
                logging.error(f"Join time not found for member {member_id} leaving channel {channel_id}. Cannot update co-occurrence.")
                # Clean up state anyway
                if channel_id in self.channel_users[guild_id] and member_id in self.channel_users[guild_id][channel_id]:
                     del self.channel_users[guild_id][channel_id][member_id]
                return

            # Calculate duration this member was in channel with others
            duration_spent = (now - join_time).total_seconds()
            logging.debug(f"Member {member_id} recorded leaving channel {channel_id} at {now}. Join time was {join_time}. Duration: {duration_spent:.2f}s. Users before leaving: {current_users_in_channel}")

            for other_member_id in current_users_in_channel:
                if other_member_id == member_id: continue

                other_join_time = self.channel_users[guild_id][channel_id].get(other_member_id)
                if other_join_time:
                    # Determine the actual overlapping time window
                    overlap_start = max(join_time, other_join_time)
                    overlap_end = now
                    overlap_duration = max(0, (overlap_end - overlap_start).total_seconds())

                    if overlap_duration > 0:
                        pair = tuple(sorted((member_id, other_member_id)))
                        # Use .get() for safer update, though defaultdict should handle it
                        current_co_occurrence = self.co_occurrence_stats[guild_id].get(pair, 0.0)
                        self.co_occurrence_stats[guild_id][pair] = current_co_occurrence + overlap_duration
                        logging.debug(f"Updated co-occurrence for pair {pair} by {overlap_duration:.2f}s in guild {guild_id}. Total: {self.co_occurrence_stats[guild_id][pair]:.2f}s")
                    else:
                         logging.debug(f"No overlap duration calculated for pair ({member_id}, {other_member_id}) during leave event.")
                else:
                    logging.warning(f"Could not find join time for other_member_id {other_member_id} in channel {channel_id} while processing leave for {member_id}.")

            # Remove the member from the channel tracking *after* calculations
            if channel_id in self.channel_users[guild_id] and member_id in self.channel_users[guild_id][channel_id]:
                del self.channel_users[guild_id][channel_id][member_id]
            # Clean up channel entry if empty
            if channel_id in self.channel_users[guild_id] and not self.channel_users[guild_id][channel_id]:
                del self.channel_users[guild_id][channel_id]

    @commands.Cog.listener()
    async def on_ready(self):
        # Initialize stats for guilds the bot is already in
        logging.info("StatsCog processing on_ready...")
        for guild in self.bot.guilds:
            self._initialize_guild_stats(guild.id)
            # Populate initial voice state
            for channel in guild.voice_channels:
                if channel.id in EXCLUDED_VOICE_CHANNEL_IDS:
                    continue
                for member in channel.members:
                    if member.bot: continue
                    if guild.id not in self.voice_activity or member.id not in self.voice_activity[guild.id]:
                         self.voice_activity[guild.id][member.id] = datetime.now()
                         logging.info(f"Tracking pre-existing member {get_preferred_name(member)} ({member.id}) in voice channel {channel.name} ({channel.id}) in guild {guild.id}")
                         # Also update channel_users state
                         if channel.id not in self.channel_users[guild.id] or member.id not in self.channel_users[guild.id][channel.id]:
                            self.channel_users[guild.id][channel.id][member.id] = datetime.now()
                            logging.debug(f"Initialized channel_users state for pre-existing member {member.id} in channel {channel.id}")

    @commands.Cog.listener()
    async def on_guild_join(self, guild):
        logging.info(f"Joined guild {guild.name} ({guild.id}). Initializing stats.")
        self._initialize_guild_stats(guild.id)

    @commands.Cog.listener()
    async def on_guild_remove(self, guild):
        logging.info(f"Left guild {guild.name} ({guild.id}). Cleaning up stats.")
        # Consider keeping data vs deleting? For now, let's keep it but remove runtime tracking.
        self.voice_activity.pop(guild.id, None)
        self.channel_users.pop(guild.id, None)
        # Maybe save one last time?
        # await self.db_manager.save_voice_stats(self.voice_stats)
        # await self.db_manager.save_co_occurrence_stats(self.co_occurrence_stats)


    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        """Tracks voice channel joins/leaves and updates stats."""
        if member.bot:
            return

        guild = member.guild
        guild_id = guild.id
        member_id = member.id
        now = datetime.now()

        # --- debugging state --- 
        # logging.debug(f"VSU Event: Member {member_id} ({get_preferred_name(member)}) Guild {guild_id}")
        # logging.debug(f"  Before: Channel={before.channel.id if before.channel else None}, Mute={before.self_mute}, Deafen={before.self_deaf}")
        # logging.debug(f"  After:  Channel={after.channel.id if after.channel else None}, Mute={after.self_mute}, Deafen={after.self_deaf}")
        # logging.debug(f"  Current voice_activity state for guild: {self.voice_activity.get(guild_id)}")
        # logging.debug(f"  Current channel_users state for guild: {self.channel_users.get(guild_id)}")
        # --- end debugging state ---

        self._initialize_guild_stats(guild_id) # Ensure data structures exist

        # Check if channel change involves excluded channels
        before_channel_id = before.channel.id if before.channel else None
        after_channel_id = after.channel.id if after.channel else None

        is_before_excluded = before_channel_id in EXCLUDED_VOICE_CHANNEL_IDS
        is_after_excluded = after_channel_id in EXCLUDED_VOICE_CHANNEL_IDS

        # Case 1: Joining a voice channel (from no channel or excluded channel to a tracked channel)
        if (before_channel_id is None or is_before_excluded) and (after_channel_id is not None and not is_after_excluded):
            logging.info(f"Member {get_preferred_name(member)} ({member_id}) joined voice channel {after.channel.name} ({after_channel_id}) in guild {guild_id}")
            self.voice_activity[guild_id][member_id] = now
            self._update_co_occurrence(guild_id, after_channel_id, member_id, 'join')

        # Case 2: Leaving a voice channel (from tracked channel to no channel or excluded channel)
        elif (before_channel_id is not None and not is_before_excluded) and (after_channel_id is None or is_after_excluded):
            logging.info(f"Member {get_preferred_name(member)} ({member_id}) left voice channel {before.channel.name} ({before_channel_id}) in guild {guild_id}")
            join_time = self.voice_activity[guild_id].pop(member_id, None)
            if join_time:
                duration_seconds = (now - join_time).total_seconds()
                if duration_seconds < 0 : # Clock sync issue?
                     logging.warning(f"Calculated negative duration ({duration_seconds}s) for {member_id} in guild {guild_id}. Ignoring.")
                else:
                    self._update_stats(guild_id, member_id, duration_seconds)
                    logging.debug(f"Recorded {duration_seconds:.2f}s for member {member_id} leaving channel {before_channel_id}")
                    # Use a try-except block for extra safety around the KeyError source
                    try:
                        self._update_co_occurrence(guild_id, before_channel_id, member_id, 'leave')
                    except KeyError as e:
                        logging.error(f"KeyError during co-occurrence update (leave) for {member_id} in channel {before_channel_id}: {e}", exc_info=True)
            else:
                logging.warning(f"Member {member_id} left voice channel {before_channel_id} but join time was not tracked.")
                # Still try to update co-occurrence state if possible, might clean up stale entries
                try:
                    self._update_co_occurrence(guild_id, before_channel_id, member_id, 'leave')
                except KeyError as e:
                    logging.error(f"KeyError during co-occurrence update (leave, untracked join) for {member_id} in channel {before_channel_id}: {e}", exc_info=True)


        # Case 3: Switching voice channels (from tracked to tracked)
        elif (before_channel_id is not None and not is_before_excluded) and \
             (after_channel_id is not None and not is_after_excluded) and \
             before_channel_id != after_channel_id:
            logging.info(f"Member {get_preferred_name(member)} ({member_id}) switched from {before.channel.name} ({before_channel_id}) to {after.channel.name} ({after_channel_id}) in guild {guild_id}")
            join_time = self.voice_activity[guild_id].get(member_id) # Don't pop yet
            if join_time:
                duration_seconds = (now - join_time).total_seconds()
                if duration_seconds < 0 :
                     logging.warning(f"Calculated negative duration ({duration_seconds}s) for {member_id} switching in guild {guild_id}. Ignoring duration update for leave part.")
                else:
                    self._update_stats(guild_id, member_id, duration_seconds)
                    logging.debug(f"Recorded {duration_seconds:.2f}s for member {member_id} leaving channel {before_channel_id} during switch")
                    # Update co-occurrence for the channel they left
                    try:
                        self._update_co_occurrence(guild_id, before_channel_id, member_id, 'leave')
                    except KeyError as e:
                         logging.error(f"KeyError during co-occurrence update (switch-leave) for {member_id} in channel {before_channel_id}: {e}", exc_info=True)
            else:
                 logging.warning(f"Member {member_id} switched channels, but join time was not tracked for channel {before_channel_id}. Handling leave part of switch.")
                 # Still try to update co-occurrence state for the channel they left
                 try:
                     self._update_co_occurrence(guild_id, before_channel_id, member_id, 'leave')
                 except KeyError as e:
                     logging.error(f"KeyError during co-occurrence update (switch-leave, untracked join) for {member_id} in channel {before_channel_id}: {e}", exc_info=True)

            # Reset join time for the new channel
            self.voice_activity[guild_id][member_id] = now
            # Update co-occurrence for the channel they joined
            self._update_co_occurrence(guild_id, after_channel_id, member_id, 'join')

        # Case 4: Moving between tracked and excluded channels
        elif before_channel_id != after_channel_id and (is_before_excluded != is_after_excluded):
             # This logic is covered by cases 1 and 2 implicitly
             pass # Logging handled within those cases

        # Case 5: Mute/Deafen/Stream state changes (within the same tracked channel)
        elif before_channel_id == after_channel_id and (before_channel_id is not None and not is_before_excluded):
            # Currently, we don't track mute/deafen time separately.
            # If needed, logic could be added here.
            pass
            # logging.debug(f"Member {member_id} changed state (mute/deaf/stream) in channel {before_channel_id}.")

        # --- debugging state --- 
        # logging.debug(f"VSU End: Member {member_id}. State after processing:")
        # logging.debug(f"  voice_activity: {self.voice_activity.get(guild_id)}")
        # logging.debug(f"  channel_users: {self.channel_users.get(guild_id)}")
        # logging.debug(f"  voice_stats entry for member: {self.voice_stats.get(guild_id, {}).get(member_id)}")
        # logging.debug(f"  Co-occurrence stats for guild: {self.co_occurrence_stats.get(guild_id)}")
        # --- end debugging state ---


    @tasks.loop(minutes=5)
    async def save_stats(self):
        """Periodically saves accumulated stats to the database."""
        logging.info("Periodic save task started.")
        # Create copies to avoid issues if data is modified during save
        stats_to_save = self.voice_stats.copy()
        co_occurrence_to_save = self.co_occurrence_stats.copy()

        if not stats_to_save and not co_occurrence_to_save:
             logging.info("No stats data to save.")
             return

        try:
            if stats_to_save:
                await self.db_manager.save_voice_stats(stats_to_save)
            if co_occurrence_to_save:
                await self.db_manager.save_co_occurrence_stats(co_occurrence_to_save)
            logging.info("Periodic save task finished.")
        except Exception as e:
            logging.error(f"Error during periodic save: {e}", exc_info=True)

    @save_stats.before_loop
    async def before_save_stats(self):
        await self.bot.wait_until_ready()
        logging.info("Database save loop waiting for bot readiness... Done.")


    def schedule_reports(self):
        """Sets up APScheduler jobs for daily, weekly, monthly reports."""
        self.scheduler = asyncio.get_event_loop().create_task(self._run_scheduler())

    async def _run_scheduler(self):
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        from apscheduler.triggers.cron import CronTrigger

        scheduler = AsyncIOScheduler(timezone=timezone('Australia/Sydney'))

        # --- Periodic Stat Reports ---
        scheduler.add_job(self.send_periodic_report, CronTrigger(hour=0, minute=5), args=['daily'], id='daily_report', replace_existing=True)
        scheduler.add_job(self.send_periodic_report, CronTrigger(day_of_week='mon', hour=0, minute=10), args=['weekly'], id='weekly_report', replace_existing=True)
        scheduler.add_job(self.send_periodic_report, CronTrigger(day=1, hour=0, minute=15), args=['monthly'], id='monthly_report', replace_existing=True)
        scheduler.add_job(self.send_periodic_report, CronTrigger(month=1, day=1, hour=0, minute=20), args=['yearly'], id='yearly_report', replace_existing=True)
        
        # --- Daily Co-occurrence Heatmap Report ---
        scheduler.add_job(self.send_daily_heatmap_report, CronTrigger(hour=0, minute=25), id='daily_heatmap', replace_existing=True)

        # --- Reset Periodic Stats --- (Run shortly after midnight)
        scheduler.add_job(self.reset_periodic_stats, CronTrigger(hour=0, minute=1), args=['daily'], id='reset_daily', replace_existing=True)
        scheduler.add_job(self.reset_periodic_stats, CronTrigger(day_of_week='mon', hour=0, minute=1), args=['weekly'], id='reset_weekly', replace_existing=True)
        scheduler.add_job(self.reset_periodic_stats, CronTrigger(day=1, hour=0, minute=1), args=['monthly'], id='reset_monthly', replace_existing=True)
        scheduler.add_job(self.reset_periodic_stats, CronTrigger(month=1, day=1, hour=0, minute=1), args=['yearly'], id='reset_yearly', replace_existing=True)

        scheduler.start()
        logging.info("Report scheduler started with jobs.")
        self.bot.report_scheduler = scheduler # Store for potential shutdown


    async def reset_periodic_stats(self, period: str):
        """Resets the stats for a specific period (daily, weekly, etc.)."""
        logging.info(f"Resetting '{period}' statistics for all guilds.")
        current_time_utc = datetime.utcnow()
        
        # Add a small buffer to avoid race conditions around midnight/week/month changes
        await asyncio.sleep(5) 

        for guild_id, members in self.voice_stats.items():
            for member_id, stats in members.items():
                if period in stats:
                    logging.debug(f"Resetting {period} stats for member {member_id} in guild {guild_id}. Old value: {stats[period]}")
                    stats[period] = 0.0 # Reset the specific period counter
        logging.info(f"Finished resetting '{period}' statistics.")
        # Save immediately after reset to persist the zeroed values
        await self.save_stats()


    async def send_periodic_report(self, period: str):
        """Generates and sends the report for the specified period to each guild."""
        logging.info(f"Generating '{period}' reports for all guilds.")
        await self.bot.wait_until_ready() # Ensure bot is connected

        # Make sure stats are saved before generating report
        await self.save_stats() 
        await asyncio.sleep(2) # Small delay to ensure save completes

        # Reload stats from DB to ensure we report based on saved data, 
        # especially relevant if the reset happened just before the report.
        # Alternatively, work with a copy taken *before* the reset? 
        # Let's use the current in-memory stats for simplicity, assuming reset/report timings are sane.
        # voice_stats_for_report = self.db_manager.load_voice_stats()

        try:
            for guild in self.bot.guilds:
                guild_id = guild.id
                logging.info(f"Generating {period} report for guild {guild.name} ({guild_id})")

                try:
                    guild_stats = self.voice_stats.get(guild_id)
                    if not guild_stats:
                        logging.warning(f"No voice stats found for guild {guild_id} when generating {period} report.")
                        continue

                    # Filter stats for the period, excluding those with 0 time
                    period_stats = {
                        mid: stats
                        for mid, stats in guild_stats.items()
                        if stats.get(period, 0) > 0
                    }

                    if not period_stats:
                         logging.info(f"No activity found for period '{period}' in guild {guild_id}. Skipping report.")
                         await send_to_command_channel(
                             self.bot, guild_id,
                             content=f"{period.capitalize()} 语音活动报告：本时段内无成员在线。"
                         )
                         continue

                    chart_buffer = await generate_periodic_chart(guild, period_stats, period)

                    if chart_buffer:
                        period_map = {'daily': '每日', 'weekly': '每周', 'monthly': '每月', 'yearly': '年度'}
                        report_title = f"{guild.name} {period_map.get(period, period.capitalize())} 语音活动报告"

                        embed = discord.Embed(title=report_title, color=discord.Color.blue())
                        embed.set_image(url=f"attachment://{period}_stats.png")
                        embed.timestamp = datetime.now()

                        file = discord.File(chart_buffer, filename=f"{period}_stats.png")
                        await send_to_command_channel(self.bot, guild_id, embed=embed, file=file)
                        logging.info(f"Sent {period} report for guild {guild_id}")
                    else:
                        logging.error(f"Failed to generate {period} chart for guild {guild_id}")
                        await send_to_command_channel(
                            self.bot, guild_id,
                            content=f"生成 {period.capitalize()} 语音活动图表时出错。"
                        )
                except Exception as e:
                    logging.error(f"Error processing {period} report for guild {guild_id}: {e}", exc_info=True)
                    try:
                        await send_to_command_channel(
                            self.bot, guild_id,
                            content=f"生成 {period.capitalize()} 语音活动报告时发生错误: {str(e)[:100]}..."
                        )
                    except:
                        logging.error(f"Failed to send error message to guild {guild_id}")
        except Exception as e:
            logging.error(f"Critical error in {period} report task: {e}", exc_info=True)

    async def send_daily_heatmap_report(self):
        """Generates and sends the daily co-occurrence heatmap report to each guild."""
        logging.info("Generating Daily Co-occurrence Heatmap reports for all guilds.")
        await self.bot.wait_until_ready() # Ensure bot is connected

        # Make sure stats are saved before generating report
        await self.save_stats()
        await asyncio.sleep(2) # Small delay to ensure save completes

        # Consider reloading co-occurrence stats from DB for consistency?
        # Using in-memory stats for now.

        try:
            for guild in self.bot.guilds:
                guild_id = guild.id
                logging.info(f"Generating daily heatmap report for guild {guild.name} ({guild_id})")

                try:
                    guild_co_occurrence = self.co_occurrence_stats.get(guild_id)
                    if not guild_co_occurrence:
                        logging.info(f"No co-occurrence data for guild {guild_id}. Skipping daily heatmap report.")
                        # Optionally send a message? 
                        # await send_to_command_channel(self.bot, guild_id, content="每日关系热力图：无数据可生成。")
                        continue

                    # Generate absolute heatmap
                    abs_heatmap_buffer = await generate_co_occurrence_heatmap(guild, guild_co_occurrence, relative=False)
                    
                    # Generate relative heatmap
                    rel_heatmap_buffer = await generate_co_occurrence_heatmap(guild, guild_co_occurrence, relative=True)

                    # Send both heatmaps if available
                    if abs_heatmap_buffer:
                        abs_report_title = f"{guild.name} 每日成员共同在线时长热力图 (小时)"
                        abs_embed = discord.Embed(title=abs_report_title, color=discord.Color.red())
                        abs_embed.description = "热力图显示每位成员与其他成员共同在线的绝对时长（小时）。"
                        abs_embed.set_image(url="attachment://daily_co_occurrence_abs.png")
                        abs_embed.timestamp = datetime.now()
                        abs_embed.set_footer(text="此报告每日自动生成")

                        abs_file = discord.File(abs_heatmap_buffer, filename="daily_co_occurrence_abs.png")
                        await send_to_command_channel(self.bot, guild_id, embed=abs_embed, file=abs_file)
                        logging.info(f"Sent daily absolute heatmap report for guild {guild_id}")
                    
                    # Send relative heatmap
                    if rel_heatmap_buffer:
                        # Allow some time between messages to avoid rate limiting
                        await asyncio.sleep(1)
                        
                        rel_report_title = f"{guild.name} 每日成员共同在线时间比例热力图 (%)"
                        rel_embed = discord.Embed(title=rel_report_title, color=discord.Color.blue())
                        rel_embed.description = "热力图显示每位成员与其他成员共同在线的时间占该成员总在线时间的百分比。"
                        rel_embed.set_image(url="attachment://daily_co_occurrence_rel.png")
                        rel_embed.timestamp = datetime.now()
                        rel_embed.set_footer(text="此报告每日自动生成")

                        rel_file = discord.File(rel_heatmap_buffer, filename="daily_co_occurrence_rel.png")
                        await send_to_command_channel(self.bot, guild_id, embed=rel_embed, file=rel_file)
                        logging.info(f"Sent daily relative heatmap report for guild {guild_id}")
                    
                    # If both failed, send error message
                    if not abs_heatmap_buffer and not rel_heatmap_buffer:
                        logging.error(f"Failed to generate both daily heatmaps for guild {guild_id}")
                        await send_to_command_channel(
                            self.bot, guild_id,
                            content="生成每日关系热力图时出错。"
                        )
                except Exception as e:
                    logging.error(f"Error processing heatmap for guild {guild_id}: {e}", exc_info=True)
                    try:
                        await send_to_command_channel(
                            self.bot, guild_id,
                            content=f"生成每日关系热力图时发生错误: {str(e)[:100]}..."
                        )
                    except:
                        logging.error(f"Failed to send error message to guild {guild_id}")
        except Exception as e:
            logging.error(f"Critical error in daily heatmap report task: {e}", exc_info=True)

    # --- Commands --- 

    @commands.command(name='stats')
    @check_channel()
    async def show_stats(self, ctx: commands.Context, period: str = 'daily'):
        """显示指定时间段的语音在线时长统计 (daily, weekly, monthly, yearly, total)。"""
        valid_periods = ['daily', 'weekly', 'monthly', 'yearly', 'total']
        if period.lower() not in valid_periods:
            await ctx.send(f"无效的时间段。请使用以下之一: {{ ", ".join(valid_periods) }} ", delete_after=10)
            return

        period = period.lower()
        guild = ctx.guild
        if not guild:
            await ctx.send("此命令只能在服务器内使用。")
            return

        await ctx.message.add_reaction('⏳') # Indicate processing

        guild_stats = self.voice_stats.get(guild.id)
        if not guild_stats:
            await ctx.send("尚未记录此服务器的语音统计数据。")
            await ctx.message.remove_reaction('⏳', self.bot.user)
            return

        # Filter stats for the period, excluding those with 0 time
        period_stats = {
            mid: stats
            for mid, stats in guild_stats.items()
            if stats.get(period, 0) > 0
        }

        if not period_stats:
             await ctx.send(f"在 '{period}' 时间段内没有成员语音活动记录。")
             await ctx.message.remove_reaction('⏳', self.bot.user)
             return

        chart_buffer = await generate_periodic_chart(guild, period_stats, period)

        if chart_buffer:
            period_map = {'daily': '今日', 'weekly': '本周', 'monthly': '本月', 'yearly': '今年', 'total': '总计'}
            title = f'{guild.name} {period_map.get(period, period.capitalize())} 语音在线时长'
            embed = discord.Embed(title=title, color=discord.Color.green())
            embed.set_image(url=f"attachment://{period}_stats_cmd.png")
            embed.timestamp = datetime.now()
            file = discord.File(chart_buffer, filename=f"{period}_stats_cmd.png")
            await ctx.send(embed=embed, file=file)
        else:
            await ctx.send("生成统计图表时出错。")

        try:
             await ctx.message.remove_reaction('⏳', self.bot.user)
             await ctx.message.add_reaction('✅')
        except discord.HTTPException:
            pass # Ignore if message was deleted or reaction couldn't be added

    @commands.command(name='relationships', aliases=['rel', 'network'])
    @check_channel()
    async def show_relationships(self, ctx: commands.Context):
        """显示成员关系网络图 (基于共同在线时长)。包含总时长Top10和本周活跃Top10用户。"""
        guild = ctx.guild
        if not guild:
            await ctx.send("此命令只能在服务器内使用。")
            return

        await ctx.message.add_reaction('⏳')

        guild_co_occurrence = self.co_occurrence_stats.get(guild.id)
        guild_voice_stats = self.voice_stats.get(guild.id)

        if not guild_co_occurrence:
            await ctx.send("尚未记录此服务器的共同在线数据。")
            await ctx.message.remove_reaction('⏳', self.bot.user)
            return
        
        # Get weekly stats, default to empty dict if no stats for guild
        weekly_stats = {mid: stats.get('weekly', 0) for mid, stats in (guild_voice_stats or {}).items()}

        # Call the graph function with both datasets
        graph_buffer = await generate_relationship_network_graph(guild, guild_co_occurrence, weekly_stats)

        if graph_buffer:
            embed = discord.Embed(title=f"{guild.name} 成员关系网络图", color=discord.Color.blue())
            embed.description = "包含总时长Top10和本周活跃Top10用户。连线粗细/深浅代表成员共同在线时长。"
            embed.set_image(url="attachment://relationship_network.png")
            embed.timestamp = datetime.now()
            file = discord.File(graph_buffer, filename="relationship_network.png")
            await ctx.send(embed=embed, file=file)
        else:
            await ctx.send("生成关系网络图时出错或没有足够的数据/用户满足条件。")

        try:
             await ctx.message.remove_reaction('⏳', self.bot.user)
             await ctx.message.add_reaction('✅')
        except discord.HTTPException:
            pass

    @commands.command(name='heatmap', aliases=['heat', 'matrix'], brief="显示成员共同在线时长热力图 (模式: abs/rel/both)。")
    @check_channel()
    async def show_heatmap(self, ctx: commands.Context, mode: str = 'absolute'):
        """显示成员共同在线时长热力图。
        
        参数:
            mode: 热力图模式 
                'absolute'/'abs' (默认): 显示绝对共同在线时长（小时）
                'relative'/'rel': 显示相对共同在线时间百分比
                'both': 同时显示绝对和相对两种热力图
        """
        guild = ctx.guild
        if not guild:
            await ctx.send("此命令只能在服务器内使用。")
            return
        
        logging.info(f"[Heatmap Cmd] Received heatmap command for guild {guild.id} (Mode: {mode})")

        await ctx.message.add_reaction('⏳')

        logging.debug("[Heatmap Cmd] Fetching co-occurrence data...")
        guild_co_occurrence = self.co_occurrence_stats.get(guild.id)

        if not guild_co_occurrence:
            logging.warning(f"[Heatmap Cmd] No co-occurrence data found for guild {guild.id}")
            await ctx.send("尚未记录此服务器的共同在线数据。")
            try:
                await ctx.message.remove_reaction('⏳', self.bot.user)
            except discord.HTTPException:
                pass
            return
        
        logging.debug(f"[Heatmap Cmd] Co-occurrence data fetched. Count: {len(guild_co_occurrence)} pairs. Preparing to generate...")

        # Process mode parameter
        mode = mode.lower()
        show_absolute = mode in ['absolute', 'abs', 'both']
        show_relative = mode in ['relative', 'rel', 'both']
        
        # If invalid mode, default to absolute
        if not show_absolute and not show_relative:
            show_absolute = True
            await ctx.send("未知的模式选项。使用默认的绝对时间模式。\n有效选项: `absolute`/`abs`, `relative`/`rel`, `both`", delete_after=10)

        try:
            # Generate requested heatmaps
            abs_heatmap_buffer = None
            rel_heatmap_buffer = None
            
            if show_absolute:
                logging.info("[Heatmap Cmd] Generating absolute heatmap...")
                abs_heatmap_buffer = await generate_co_occurrence_heatmap(guild, guild_co_occurrence, relative=False)
                logging.info("[Heatmap Cmd] Absolute heatmap generation finished.")
                
            if show_relative: 
                logging.info("[Heatmap Cmd] Generating relative heatmap...")
                rel_heatmap_buffer = await generate_co_occurrence_heatmap(guild, guild_co_occurrence, relative=True)
                logging.info("[Heatmap Cmd] Relative heatmap generation finished.")
            
            # Send absolute heatmap if requested and available
            if show_absolute and abs_heatmap_buffer:
                logging.debug("[Heatmap Cmd] Sending absolute heatmap...")
                abs_title = f"{guild.name} 成员共同在线时长热力图"
                abs_description = "热力图显示每位成员与其他成员共同在线的绝对时长（小时）。"
                abs_filename = "co_occurrence_abs.png"
                
                abs_embed = discord.Embed(title=abs_title, color=discord.Color.orange())
                abs_embed.description = abs_description
                abs_embed.set_image(url=f"attachment://{abs_filename}")
                abs_embed.timestamp = datetime.now()
                abs_file = discord.File(abs_heatmap_buffer, filename=abs_filename)
                
                await ctx.send(embed=abs_embed, file=abs_file)
                logging.info(f"[Heatmap Cmd] Absolute heatmap sent for guild {guild.id}.")
            
            # Send relative heatmap if requested and available
            if show_relative and rel_heatmap_buffer:
                # Add a slight delay between messages to avoid rate limiting
                if show_absolute and abs_heatmap_buffer:
                    await asyncio.sleep(1)
                
                logging.debug("[Heatmap Cmd] Sending relative heatmap...")
                rel_title = f"{guild.name} 成员共同在线时间比例热力图"
                rel_description = "热力图显示每位成员与其他成员共同在线的时间占该成员总在线时间的百分比。"
                rel_filename = "co_occurrence_rel.png"
                
                rel_embed = discord.Embed(title=rel_title, color=discord.Color.blue())
                rel_embed.description = rel_description
                rel_embed.set_image(url=f"attachment://{rel_filename}")
                rel_embed.timestamp = datetime.now()
                rel_file = discord.File(rel_heatmap_buffer, filename=rel_filename)
                
                await ctx.send(embed=rel_embed, file=rel_file)
                logging.info(f"[Heatmap Cmd] Relative heatmap sent for guild {guild.id}.")
            
            # If all requested heatmaps failed
            if (show_absolute and not abs_heatmap_buffer) and (show_relative and not rel_heatmap_buffer):
                logging.warning("[Heatmap Cmd] All requested heatmaps failed to generate.")
                await ctx.send("生成热力图时出错或没有足够的数据。热力图需要至少两位成员有共同在线记录。")
                
        except Exception as e:
            logging.error(f"[Heatmap Cmd] Unexpected error during heatmap command execution: {e}", exc_info=True)
            await ctx.send(f"生成热力图时发生严重错误: {str(e)[:100]}...")
        finally:
            logging.debug("[Heatmap Cmd] Removing reaction...")
            try:
                await ctx.message.remove_reaction('⏳', self.bot.user)
                await ctx.message.add_reaction('✅')
            except discord.HTTPException:
                logging.warning("[Heatmap Cmd] Failed to remove/add reaction.")
                pass

async def setup(bot: commands.Bot):
    db_manager = DatabaseManager() # Create instance here
    await bot.add_cog(StatsCog(bot, db_manager)) 