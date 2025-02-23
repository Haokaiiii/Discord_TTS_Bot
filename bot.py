import os
import discord
from discord.ext import commands, tasks
from gtts import gTTS
import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import json
import seaborn as sns
import matplotlib.pyplot as plt
import matplotlib.font_manager as fm
import glob
import time
import backoff
import aiohttp
import hashlib
from asyncio import Lock
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from pytz import timezone
import numpy as np
import pandas as pd
from pymongo import MongoClient
from dotenv import load_dotenv
from aiohttp import web
import io

DEBOUNCE_TIME = 2.0  # 等 2 秒看看用户还会不会继续切换
pending_switch_tasks = {}  # 存储 (guild_id, member_id) -> asyncio.Task

# 加载环境变量
load_dotenv()

# 设置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# ---------------- 字体与样式设置 ----------------

font_path = '/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc'
if os.path.exists(font_path):
    font_prop = fm.FontProperties(fname=font_path)
    plt.rcParams['font.sans-serif'] = [font_prop.get_name()]
    plt.rcParams['font.family'] = 'sans-serif'
    plt.rcParams['axes.unicode_minus'] = False
    sns.set_theme(style="whitegrid", font=font_prop.get_name())
else:
    logging.warning(f"字体文件 {font_path} 不存在。将使用默认字体。")
    sns.set_theme(style="whitegrid")

# ---------------- 初始化 Bot ----------------
intents = discord.Intents.default()
intents.voice_states = True
intents.guilds = True
intents.messages = True
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix='!', intents=intents)

# ---------------- 全局变量与数据结构 ----------------
guild_voice_clients = {}
guild_tts_queues = {}
guild_tts_tasks = {}

ALLOWED_COMMAND_CHANNEL_ID = 1342752925533081630
EXCLUDED_VOICE_CHANNEL_IDS = set()

executor = ThreadPoolExecutor(max_workers=8)

# MongoDB 配置
MONGODB_URI = os.getenv('MONGODB_URI')
if not MONGODB_URI:
    logging.error("未找到 MONGODB_URI 环境变量。请设置后重试。")
    exit(1)

client = MongoClient(MONGODB_URI)
db = client['discord_bot']

voice_stats_col = db['voice_stats']
co_occurrence_col = db['co_occurrence_stats']

voice_activity = {}       # {guild_id: {member_id: join_time}}
voice_stats = {}          # {guild_id: {member_id: {total, daily, weekly, monthly, yearly}}}
co_occurrence_stats = {}  # {guild_id: {(m1, m2): seconds}, where (m1, m2) is tuple(m1<m2)}
channel_users = {}        # {guild_id: {channel_id: {member_id: join_time}}}

FFMPEG_EXECUTABLE = "ffmpeg"
TTS_CACHE_DIR = "tts_cache"
os.makedirs(TTS_CACHE_DIR, exist_ok=True)

save_lock = Lock()

# 事件处理锁 (防止 on_voice_state_update 并发执行导致混乱)
voice_event_lock = Lock()

# 语音连接锁 (确保连接/断开等逻辑不并发冲突)
voice_connection_lock = Lock()

scheduler = AsyncIOScheduler(timezone=timezone('Australia/Sydney'))

# 添加本地备份路径
BACKUP_DIR = "data_backup"
os.makedirs(BACKUP_DIR, exist_ok=True)

# --- 新增/修改 ---
# 用于防止并发重连的标记
reconnecting_guilds = set()

command_channel = None  # 用于存储命令频道的引用

def save_local_backup(data, filename):
    """保存数据到本地备份文件"""
    backup_path = os.path.join(BACKUP_DIR, filename)
    with open(backup_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False)
    logging.info(f"已创建本地备份: {backup_path}")

def load_local_backup(filename):
    """从本地备份文件加载数据"""
    backup_path = os.path.join(BACKUP_DIR, filename)
    if os.path.exists(backup_path):
        with open(backup_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    return None

@backoff.on_exception(
    backoff.expo,
    Exception,
    max_tries=5,
    max_time=300
)
async def save_voice_stats():
    async with save_lock:
        try:
            # 先创建本地备份
            backup_data = {
                str(guild_id): {
                    str(member_id): data
                    for member_id, data in members.items()
                }
                for guild_id, members in voice_stats.items()
            }
            save_local_backup(backup_data, f"voice_stats_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
            
            # 尝试保存到MongoDB
            for guild_id, members in voice_stats.items():
                serialized_members = {
                    str(member_id): data
                    for member_id, data in members.items()
                }
                voice_stats_col.update_one(
                    {'guild_id': guild_id},
                    {'$set': {'members': serialized_members}},
                    upsert=True
                )
            logging.info("语音统计数据已保存到MongoDB。")
        except Exception as e:
            logging.error(f"保存语音统计数据时出错: {e}")
            raise  # 让 backoff 进行重试

@backoff.on_exception(
    backoff.expo,
    Exception,
    max_tries=5,
    max_time=300
)
async def save_co_occurrence_stats():
    async with save_lock:
        try:
            # 先创建本地备份
            backup_data = {
                str(guild_id): {
                    f"{m1},{m2}": duration
                    for (m1, m2), duration in pairs.items()
                }
                for guild_id, pairs in co_occurrence_stats.items()
            }
            save_local_backup(backup_data, f"co_occurrence_stats_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
            
            # 尝试保存到MongoDB
            for guild_id, pairs in co_occurrence_stats.items():
                serialized_pairs = {
                    f"{m1},{m2}": duration
                    for (m1, m2), duration in pairs.items()
                }
                co_occurrence_col.update_one(
                    {'guild_id': guild_id},
                    {'$set': {'pairs': serialized_pairs}},
                    upsert=True
                )
            logging.info("共同在线统计数据已保存。")
        except Exception as e:
            logging.error(f"保存共同在线统计数据时出错: {e}")
            raise  # 让 backoff 进行重试

def load_voice_stats():
    """从 MongoDB 加载语音统计数据"""
    try:
        # 先尝试从本地备份加载
        backup_files = sorted(glob.glob(os.path.join(BACKUP_DIR, "voice_stats_*.json")))
        if backup_files:
            latest_backup = backup_files[-1]
            backup_data = load_local_backup(os.path.basename(latest_backup))
            if backup_data:
                for guild_id_str, members in backup_data.items():
                    guild_id = int(guild_id_str)
                    voice_stats[guild_id] = {
                        int(member_id): data
                        for member_id, data in members.items()
                    }
                logging.info("已从本地备份加载语音统计数据。")
                return

        # 如果没有本地备份或加载失败，从 MongoDB 加载
        for doc in voice_stats_col.find():
            guild_id = doc['guild_id']
            members = doc.get('members', {})
            voice_stats[guild_id] = {
                int(member_id): data
                for member_id, data in members.items()
            }
        logging.info("已从 MongoDB 加载语音统计数据。")
    except Exception as e:
        logging.error(f"加载语音统计数据时出错: {e}")

def load_co_occurrence_stats():
    """从 MongoDB 加载共同在线统计数据"""
    try:
        for doc in co_occurrence_col.find():
            guild_id = doc['guild_id']
            pairs = doc.get('pairs', {})
            co_occurrence_stats[guild_id] = {}
            for pair_str, duration in pairs.items():
                m1, m2 = map(int, pair_str.split(','))
                co_occurrence_stats[guild_id][(m1, m2)] = duration
        logging.info("已从 MongoDB 加载共同在线统计数据。")
    except Exception as e:
        logging.error(f"加载共同在线统计数据时出错: {e}")

@bot.event
async def on_member_update(before, after):
    # 如果昵称改变，可以播报 TTS
    if before.nick != after.nick:
        guild = after.guild
        guild_id = guild.id
        member_id = after.id
        name_before = get_preferred_name(before)
        name_after = get_preferred_name(after)
        logging.info(f"成员 {name_before} 更改了昵称为 {name_after}")

        # 如果成员在语音频道中，可以选择是否触发 TTS
        voice_client = guild_voice_clients.get(guild_id)
        if voice_client and voice_client.channel:
            if member_id in channel_users.get(guild_id, {}).get(voice_client.channel.id, {}):
                message = f"{name_after} 更改了昵称！"
                await queue_tts(guild, voice_client.channel, message)

@bot.event
async def on_ready():
    global command_channel
    logging.info(f'已登录为 {bot.user}')
    logging.info(f"当前已加载命令: {[cmd.name for cmd in bot.commands]}")

    # 初始化命令频道
    for guild in bot.guilds:
        channel = guild.get_channel(ALLOWED_COMMAND_CHANNEL_ID)
        if channel:
            command_channel = channel  # 正确设置命令频道
            logging.info(f"已找到并设置命令频道: {channel.name}")
            break

    if not command_channel:
        logging.error("未找到命令频道，请检查 ALLOWED_COMMAND_CHANNEL_ID 是否正确设置。")
        return

    load_voice_stats()
    load_co_occurrence_stats()
    
    for guild in bot.guilds:
        try:
            # 强制刷新成员缓存
            await guild.chunk()
            members = guild.members
            logging.info(f"已获取并缓存 {guild.name} 的所有成员，总数: {len(members)}")
        except Exception as e:
            logging.error(f"获取服务器 {guild.name} 的成员时出错: {e}")
            members = guild.members

        if guild.id not in guild_tts_queues:
            guild_tts_queues[guild.id] = asyncio.Queue()
            guild_tts_tasks[guild.id] = asyncio.create_task(process_guild_tts_queue(guild.id))

        channel_users[guild.id] = {}
        for member in members:
            if member.voice and not member.bot:
                guild_id = guild.id
                member_id = member.id
                now = datetime.utcnow()
                voice_activity.setdefault(guild_id, {})[member_id] = now
                ch_id = member.voice.channel.id
                channel_users[guild_id].setdefault(ch_id, {})
                channel_users[guild_id][ch_id][member_id] = now
                logging.info(f"初始化用户 {get_preferred_name(member)} 在语音频道 {member.voice.channel.name}")

    logging.info("所有服务器的 TTS 处理任务已启动。")
    scheduler.start()
    schedule_reports()
    save_stats.start()
    cleanup_tts_files.start()
    check_voice_connections.start()
    logging.info("定期保存数据、报告和清理任务已启动。")

def get_preferred_name(member):
    """
    返回成员的首选显示名称：
    1. 服务器昵称 (nick)
    2. 通用昵称 (global_name) - 部分版本Discord有这个属性
    3. 用户名 (name)
    """
    if member.nick:
        return member.nick
    elif hasattr(member, 'global_name') and member.global_name:
        return member.global_name
    else:
        return member.name

def check_channel():
    async def predicate(ctx):
        is_allowed = ctx.channel.id == ALLOWED_COMMAND_CHANNEL_ID
        if not is_allowed:
            # 如果不是指定频道，直接忽略命令
            return False
        return True
    return commands.check(predicate)

@bot.command()
@check_channel()
async def check_nickname(ctx, member: discord.Member = None):
    """检查指定成员的昵称信息"""
    if member is None:
        member = ctx.author
    name = get_preferred_name(member)
    await send_to_command_channel(ctx.guild, content=f"成员 {member.id} 的首选名称: {name}")
    logging.info(f"检查成员: ID={member.id}, Username={member.name}, Nickname={member.nick}, DisplayName={member.display_name}")

@bot.event
async def on_guild_join(guild):
    logging.info(f"机器人已加入服务器: {guild.name} (ID: {guild.id})")
    if guild.id not in guild_tts_queues:
        guild_tts_queues[guild.id] = asyncio.Queue()
        guild_tts_tasks[guild.id] = asyncio.create_task(process_guild_tts_queue(guild.id))

@bot.event
async def on_guild_remove(guild):
    logging.info(f"机器人已从服务器移除: {guild.name} (ID: {guild.id})")
    voice_client = guild_voice_clients.get(guild.id)
    if voice_client:
        try:
            await voice_client.disconnect()
            del guild_voice_clients[guild.id]
            logging.info(f"已断开与服务器 '{guild.name}' 的语音连接。")
        except Exception as e:
            logging.error(f"断开语音连接时发生错误: {e}")
    tts_task = guild_tts_tasks.get(guild.id)
    if tts_task:
        tts_task.cancel()
        del guild_tts_tasks[guild.id]
    tts_queue = guild_tts_queues.get(guild.id)
    if tts_queue:
        while not tts_queue.empty():
            try:
                tts_queue.get_nowait()
                tts_queue.task_done()
            except asyncio.QueueEmpty:
                break
        del guild_tts_queues[guild.id]
    await save_voice_stats()
    await save_co_occurrence_stats()

@bot.event
async def on_disconnect():
    logging.info("机器人已断线，清理所有语音客户端并保存数据。")
    for guild_id, voice_client in list(guild_voice_clients.items()):
        if voice_client:
            try:
                await voice_client.disconnect()
                del guild_voice_clients[guild_id]
                logging.info(f"已断开与 guild ID {guild_id} 的语音连接。")
            except Exception as e:
                logging.error(f"断开语音连接时发生错误: {e}")
    await save_voice_stats()
    await save_co_occurrence_stats()

@bot.event
async def on_voice_state_update(member, before, after):
    if member.bot:
        return

    # 使用事件处理锁，确保不会并发处理多次 on_voice_state_update
    async with voice_event_lock:
        guild = member.guild
        guild_id = guild.id
        member_id = member.id

        if guild_id not in voice_activity:
            voice_activity[guild_id] = {}
        if guild_id not in voice_stats:
            voice_stats[guild_id] = {}
        if member_id not in voice_stats[guild_id]:
            voice_stats[guild_id][member_id] = {
                'total': 0,
                'daily': 0,
                'weekly': 0,
                'monthly': 0,
                'yearly': 0
            }

        now = datetime.utcnow()

        # 用户加入语音频道
        if not before.channel and after.channel:
            voice_activity[guild_id][member_id] = now
            ch_id = after.channel.id
            channel_users.setdefault(guild_id, {}).setdefault(ch_id, {})
            channel_users[guild_id][ch_id][member_id] = now
            logging.info(f"用户 {get_preferred_name(member)} 加入了 {after.channel.name} 于 {now}")
            await handle_voice_event(guild, member, before, after, event_type='join')

        # 用户离开语音频道
        elif before.channel and not after.channel:
            join_time = voice_activity[guild_id].pop(member_id, None)
            if join_time:
                duration = (now - join_time).total_seconds()
                for p in ['total','daily','weekly','monthly','yearly']:
                    voice_stats[guild_id][member_id][p] += duration

                ch_id = before.channel.id
                if guild_id in channel_users and ch_id in channel_users[guild_id]:
                    for other_id, other_join_time in list(channel_users[guild_id][ch_id].items()):
                        if other_id != member_id:
                            co_start = max(join_time, other_join_time)
                            co_duration = (now - co_start).total_seconds()
                            if co_duration > 0:
                                pair = (min(member_id, other_id), max(member_id, other_id))
                                co_occurrence_stats.setdefault(guild_id, {})
                                co_occurrence_stats[guild_id][pair] = co_occurrence_stats[guild_id].get(pair, 0) + co_duration

                    channel_users[guild_id][ch_id].pop(member_id, None)

                logging.info(f"用户 {get_preferred_name(member)} 离开了 {before.channel.name} 于 {now}, 时长: {duration}秒")

            await handle_voice_event(guild, member, before, after, event_type='leave')

        # 用户切换语音频道
        elif before.channel != after.channel:
            join_time = voice_activity[guild_id].pop(member_id, None)
            if join_time:
                duration = (now - join_time).total_seconds()
                for p in ['total','daily','weekly','monthly','yearly']:
                    voice_stats[guild_id][member_id][p] += duration

                old_ch_id = before.channel.id
                new_ch_id = after.channel.id
                # 计算旧频道共同时长
                if guild_id in channel_users and old_ch_id in channel_users[guild_id]:
                    for other_id, other_join_time in list(channel_users[guild_id][old_ch_id].items()):
                        if other_id != member_id:
                            co_start = max(join_time, other_join_time)
                            co_duration = (now - co_start).total_seconds()
                            if co_duration > 0:
                                pair = (min(member_id, other_id), max(member_id, other_id))
                                co_occurrence_stats.setdefault(guild_id, {})
                                co_occurrence_stats[guild_id][pair] = co_occurrence_stats[guild_id].get(pair, 0) + co_duration
                    channel_users[guild_id][old_ch_id].pop(member_id, None)

                voice_activity[guild_id][member_id] = now
                channel_users.setdefault(guild_id, {}).setdefault(new_ch_id, {})
                channel_users[guild_id][new_ch_id][member_id] = now

                logging.info(f"用户 {get_preferred_name(member)} 从 {before.channel.name} 切换到 {after.channel.name} 于 {now}, 时长: {duration}秒")

            await handle_voice_event(guild, member, before, after, event_type='switch')


async def handle_voice_event(guild, member, before, after, event_type):
    """Handles join/leave/switch events and queues TTS messages."""
    try:
        # 获取最新的成员信息
        member = await guild.fetch_member(member.id)
        name = get_preferred_name(member)
    except discord.NotFound:
        logging.warning(f"成员未找到或已离开: ID={member.id}")
        return
    except Exception as e:
        logging.error(f"获取成员信息时出错: {e}")
        return

    if event_type == 'switch':
        # 切频道要做 "延迟" 判断
        key = (guild.id, member.id)
        old_task = pending_switch_tasks.get(key)
        if old_task and not old_task.done():
            old_task.cancel()

        t = asyncio.create_task(delayed_switch_broadcast(guild, member, after.channel))
        pending_switch_tasks[key] = t
        return

    elif event_type == 'leave':
        message = f"{name} 滚了！"
        voice_channel = before.channel
    elif event_type == 'join':
        message = f"欢迎 {name}！"
        voice_channel = after.channel
    else:
        return

    voice_client = await get_voice_client(guild.id, voice_channel)
    if not voice_client:
        logging.error(
            f"获取或连接语音频道失败，Guild={guild.id}, Channel={voice_channel.id}"
        )
        return

    await queue_tts(guild, voice_channel, message)

async def delayed_switch_broadcast(guild, member, voice_channel):
    """延迟 DEBOUNCE_TIME 秒后，如果此任务没被取消，则执行"叛变了"TTS。"""
    try:
        await asyncio.sleep(DEBOUNCE_TIME)
    except asyncio.CancelledError:
        logging.info(f"用户 {get_preferred_name(member)} 的 switch TTS 被新的切换事件取消。")
        return

    message = f"{get_preferred_name(member)} 叛变了！"
    voice_client = await get_voice_client(guild.id, voice_channel)
    if not voice_client:
        logging.error(
            f"获取或连接语音频道失败 (switch)，Guild={guild.id}, Channel={voice_channel.id}"
        )
        return

    await queue_tts(guild, voice_channel, message)

async def queue_tts(guild, voice_channel, message):
    """把 TTS 任务提交到队列，与 process_guild_tts_queue 配合。"""
    tts_message = message
    message_hash = hashlib.md5(tts_message.encode('utf-8')).hexdigest()
    tts_path = os.path.join(TTS_CACHE_DIR, f"tts_{message_hash}.mp3")

    if not os.path.exists(tts_path) or os.path.getsize(tts_path) == 0:
        await generate_tts(tts_message, tts_path)

    await guild_tts_queues[guild.id].put({
        'guild': guild,
        'voice_channel': voice_channel,
        'message': message,
        'tts_path': tts_path,
        'member': None
    })

def has_required_permissions(channel):
    permissions = channel.permissions_for(channel.guild.me)
    return permissions.send_messages and permissions.embed_links

async def generate_tts(text, output_path):
    try:
        loop = asyncio.get_event_loop()
        tts = gTTS(text=text, lang='zh')
        await loop.run_in_executor(
            executor,
            lambda: tts.save(output_path)
        )
        logging.info(f"TTS 文件已保存到: {output_path}")
    except Exception as e:
        logging.error(f"TTS 生成失败: {e}")

async def process_guild_tts_queue(guild_id):
    """持续从 TTS 队列获取任务并播放"""
    guild = bot.get_guild(guild_id)
    if not guild:
        logging.error(f"无法获取 guild ID {guild_id}。")
        return

    while True:
        try:
            task = await guild_tts_queues[guild_id].get()
            await handle_tts_task(task)
        except asyncio.CancelledError:
            logging.info(f"TTS 队列处理任务已取消 for guild ID {guild_id}")
            break
        except Exception as e:
            logging.error(f"处理 TTS 任务时发生错误: {e}")
        finally:
            guild_tts_queues[guild_id].task_done()


# --- 新增/修改 ---
async def force_disconnect_and_wait(guild: discord.Guild, delay: float = 2.0):
    """
    强制让指定Guild的 voice_client 断开，并等待几秒，直到真正断开。
    """
    guild_id = guild.id
    vc_in_dict = guild_voice_clients.get(guild_id)
    vc_in_guild = guild.voice_client

    # 收集需要断开的 VC
    to_disconnect = set()
    if vc_in_dict and vc_in_dict.is_connected():
        to_disconnect.add(vc_in_dict)
    if vc_in_guild and vc_in_guild.is_connected():
        to_disconnect.add(vc_in_guild)

    for vc in to_disconnect:
        try:
            await vc.disconnect(force=True)
        except Exception as e:
            logging.error(f"强制断开时发生错误: {e}")

    # 从本地缓存里删掉
    guild_voice_clients.pop(guild_id, None)

    # 等待一小段时间，让 Discord 底层的断开和事件传播完成
    await asyncio.sleep(delay)

    # 再检查一次
    vc_in_guild = guild.voice_client
    if vc_in_guild and vc_in_guild.is_connected():
        logging.warning("等待后，guild.voice_client 仍保持连接，可能需要再次尝试断开。")
        try:
            await vc_in_guild.disconnect(force=True)
        except Exception as e:
            logging.error(f"第二次强制断开时发生错误: {e}")
        await asyncio.sleep(delay)

    logging.info("确认已与语音频道断开。")


# ------------------ 重写 get_voice_client，整合以上逻辑 ------------------
async def get_voice_client(guild_id: int, channel: discord.VoiceChannel):
    """
    获取或建立给定 guild 对应的语音连接。
    若已连接到不同频道，会先断开。
    若在尝试连接时遇到 "Already connected to a voice channel"，
    就执行强制清理并重试一次。
    """
    guild = bot.get_guild(guild_id)
    if not guild:
        logging.error(f"找不到 Guild ID={guild_id}")
        return None

    async with voice_connection_lock:
        # 如果已经有重连在进行，就直接跳过，避免并发竞争
        if guild_id in reconnecting_guilds:
            logging.info(f"Guild {guild_id} 的重连正在进行中，跳过本次 get_voice_client 调用。")
            return guild_voice_clients.get(guild_id, None)

        reconnecting_guilds.add(guild_id)
        try:
            voice_client = guild_voice_clients.get(guild_id)
            current_channel = voice_client.channel if voice_client and voice_client.is_connected() else None

            # (1) 如果已经连接且就在同一个频道，直接复用
            if voice_client and voice_client.is_connected() and current_channel and current_channel.id == channel.id:
                return voice_client

            # (2) 如果 voice_client 存在但频道不同，则先断开
            if voice_client and voice_client.is_connected():
                await force_disconnect_and_wait(guild, delay=2.0)

            # (3) 第一次尝试连接
            try:
                new_vc = await channel.connect()
                new_vc.last_success = time.time()
                new_vc.failed_attempts = 0
                guild_voice_clients[guild_id] = new_vc
                return new_vc
            except Exception as e:
                # (4) 如果是 "Already connected"，则立刻退频道再连一次
                if "Already connected to a voice channel" in str(e):
                    logging.warning("检测到 'Already connected' 异常，尝试强制退频道后再连一次...")
                    await force_disconnect_and_wait(guild, delay=3.0)
                    try:
                        new_vc = await channel.connect()
                        new_vc.last_success = time.time()
                        new_vc.failed_attempts = 0
                        guild_voice_clients[guild_id] = new_vc
                        return new_vc
                    except Exception as e2:
                        logging.error(f"二次重试仍然连接失败: {e2}")
                        return None
                else:
                    logging.error(f"连接到语音频道失败: {e}")
                    return None
        finally:
            reconnecting_guilds.discard(guild_id)


async def handle_tts_task(task):
    """处理 TTS 任务，包含重试机制和错误恢复"""
    guild = task['guild']
    voice_channel = task['voice_channel']
    tts_path = task['tts_path']
    max_retries = 3
    retry_delay = 2.0

    voice_client = guild_voice_clients.get(guild.id)

    for attempt in range(max_retries):
        try:
            if not voice_client or not voice_client.is_connected():
                voice_client = await get_voice_client(guild.id, voice_channel)
                if not voice_client:
                    raise RuntimeError("Failed to establish voice connection.")

            if not os.path.exists(tts_path):
                logging.error(f"TTS 文件不存在: {tts_path}")
                return

            audio_source = discord.FFmpegPCMAudio(
                tts_path,
                executable=FFMPEG_EXECUTABLE,
                options='-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5'
            )
            audio_source = discord.PCMVolumeTransformer(audio_source, volume=1.0)

            # 创建一个 Future 对象来跟踪播放完成
            play_finished = asyncio.Future()

            def after_play(error):
                if error:
                    logging.error(f"播放完成时发生错误: {error}")
                else:
                    voice_client.last_success = time.time()
                    voice_client.failed_attempts = 0
                # 直接设置 Future 的结果
                bot.loop.call_soon_threadsafe(lambda: play_finished.set_result(True))

            if not voice_client.is_playing():
                voice_client.play(audio_source, after=after_play)
                # 等待播放完成
                await play_finished
                # 创建延迟断开的任务
                bot.loop.create_task(delayed_disconnect(guild.id))
                return

            await asyncio.sleep(1.0)

        except Exception as e:
            logging.error(f"处理 TTS 任务时发生错误 (尝试 {attempt + 1}/{max_retries}): {e}")
            if voice_client:
                voice_client.failed_attempts = getattr(voice_client, 'failed_attempts', 0) + 1
            
            if attempt == max_retries - 1:
                logging.error(f"在 {max_retries} 次尝试后放弃 TTS 任务")
                return
                
            await asyncio.sleep(retry_delay * (attempt + 1))

async def delayed_disconnect(guild_id: int):
    """30秒后断开语音连接"""
    try:
        await asyncio.sleep(30)  # 等待30秒
        voice_client = guild_voice_clients.get(guild_id)
        
        if voice_client and voice_client.is_connected():
            if not voice_client.is_playing():  # 确保没有在播放其他内容
                await cleanup_voice_client(guild_id)
                logging.info(f"已自动断开与服务器 {guild_id} 的语音连接")
            else:
                logging.info(f"服务器 {guild_id} 仍在播放内容，取消自动断开")
    except Exception as e:
        logging.error(f"自动断开语音连接时发生错误: {e}")

@bot.command()
@check_channel()
async def leave(ctx):
    """让机器人离开当前语音频道"""
    guild = ctx.guild
    voice_client = guild_voice_clients.get(guild.id)
    if voice_client and voice_client.is_connected():
        try:
            await voice_client.disconnect()
            del guild_voice_clients[guild.id]
            await send_to_command_channel(ctx.guild, content="已离开语音频道。")
        except Exception as e:
            await send_to_command_channel(ctx.guild, content="无法离开语音频道。")
            logging.error(f"离开语音频道失败: {e}")
    else:
        await send_to_command_channel(ctx.guild, content="机器人当前不在任何语音频道。")

@bot.command()
@check_channel()
async def test_delete(ctx):
    """测试消息删除功能"""
    try:
        message = await send_to_command_channel(ctx.guild, content="这是一条测试消息，将在5秒后删除。")
        await asyncio.sleep(5)
        await message.delete()
    except Exception as e:
        await send_to_command_channel(ctx.guild, content=f"删除失败: {e}")

@bot.command()
@check_channel()
async def play_test(ctx):
    """播放测试音频"""
    guild = ctx.guild
    voice_client = guild_voice_clients.get(guild.id)
    if not voice_client:
        if ctx.author.voice:
            try:
                voice_client = await ctx.author.voice.channel.connect()
                guild_voice_clients[guild.id] = voice_client
            except Exception as e:
                logging.error(f"无法连接到语音频道: {e}")
                await send_to_command_channel(ctx.guild, content="无法连接到语音频道。")
                return
        else:
            await send_to_command_channel(ctx.guild, content="你当前不在任何语音频道。")
            return

    test_audio_path = os.path.join(TTS_CACHE_DIR, "test.mp3")
    if not os.path.exists(test_audio_path):
        await generate_tts("这是一个测试音频。", test_audio_path)

    try:
        if not os.path.exists(test_audio_path):
            logging.error(f"测试音频文件不存在: {test_audio_path}")
            await send_to_command_channel(ctx.guild, content="测试音频文件不存在。")
            return

        audio_source = discord.FFmpegPCMAudio(test_audio_path, executable=FFMPEG_EXECUTABLE)
        if not voice_client.is_playing():
            voice_client.play(audio_source)
            await send_to_command_channel(ctx.guild, content="正在播放测试音频。")
        else:
            await send_to_command_channel(ctx.guild, content="语音客户端正在播放其他音频。")
    except Exception as e:
        logging.error(f"播放测试音频失败: {e}")
        await send_to_command_channel(ctx.guild, content="播放测试音频失败。")


# -------------------------------------------------------------------
# 生成共同在线热图 (绝对时长)
# -------------------------------------------------------------------
@bot.command()
@check_channel()
async def show_relationships(ctx):
    """Show a heatmap of how often users are in voice channels together (绝对时长)"""
    try:
        buf, error = await generate_co_occurrence_heatmap(ctx.guild.id)
        if error:
            await send_to_command_channel(ctx.guild, content=error)
            return
        file = discord.File(buf, filename='relationships.png')
        await send_to_command_channel(ctx.guild, file=file)
    except Exception as e:
        logging.error(f"Error in show_relationships: {e}", exc_info=True)
        await send_to_command_channel(ctx.guild, content=f"Error generating relationship visualization: {str(e)}")

async def generate_co_occurrence_heatmap(guild_id):
    """Generate a heatmap showing how often users are in voice channels together,
       只考虑总时长排名前30 + daily前20 + 最近加入者。
       最终对坐标轴按照"总时长"从大到小进行排序。
    """
    try:
        stats = co_occurrence_stats.get(guild_id, {})
        if not stats:
            return None, "No co-occurrence data available."
        
        guild_voice_stat = voice_stats.get(guild_id, {})
        if not guild_voice_stat:
            return None, "No voice_stats data for this guild."

        ranked_by_total = sorted(
            guild_voice_stat.items(),
            key=lambda x: x[1]['total'],
            reverse=True
        )
        top_30_total_ids = [member_id for member_id, _ in ranked_by_total[:30]]

        ranked_by_daily = sorted(
            guild_voice_stat.items(),
            key=lambda x: x[1]['daily'],
            reverse=True
        )
        top_20_daily_ids = [member_id for member_id, _ in ranked_by_daily[:20]]

        guild_obj = bot.get_guild(guild_id)
        if guild_obj is None:
            return None, "Guild not found."

        members_with_joined = [m for m in guild_obj.members if m.joined_at]
        if members_with_joined:
            last_joined_member = max(members_with_joined, key=lambda m: m.joined_at)
            last_joined_id = last_joined_member.id
        else:
            last_joined_id = None

        selected_ids = set(top_30_total_ids + top_20_daily_ids)
        if last_joined_id is not None:
            selected_ids.add(last_joined_id)

        if len(selected_ids) < 2:
            return None, "Not enough users to generate a meaningful heatmap."

        filtered_stats = {}
        for (u1, u2), seconds in stats.items():
            if u1 in selected_ids and u2 in selected_ids:
                filtered_stats[(u1, u2)] = seconds

        if not filtered_stats:
            return None, "No co-occurrence data available after filtering."

        final_users = set()
        for (u1, u2) in filtered_stats.keys():
            final_users.add(u1)
            final_users.add(u2)
        final_users = list(final_users)

        final_users = sorted(
            final_users,
            key=lambda uid: guild_voice_stat[uid]['total'] if uid in guild_voice_stat else 0,
            reverse=True
        )

        n = len(final_users)
        matrix = np.zeros((n, n), dtype=float)
        idx_map = {uid: i for i, uid in enumerate(final_users)}

        for (u1, u2), seconds in filtered_stats.items():
            i = idx_map[u1]
            j = idx_map[u2]
            hours = seconds / 3600.0
            matrix[i][j] = hours
            matrix[j][i] = hours

        labels = []
        for uid in final_users:
            m = guild_obj.get_member(uid)
            labels.append(get_preferred_name(m) if m else str(uid))

        count = len(labels)
        fig_width = max(6, min(24, count * 0.8))
        fig_height = max(6, min(24, count * 0.8))

        plt.figure(figsize=(fig_width, fig_height))
        if count <= 10:
            tick_fontsize = 14
        elif count <= 30:
            tick_fontsize = 12
        else:
            tick_fontsize = 8

        sns.heatmap(
            matrix,
            xticklabels=labels,
            yticklabels=labels,
            cmap='rocket_r',
            annot=False,
            linewidths=.5,
            square=True,
            cbar_kws={"shrink": .8, "label": "共同在线时长（小时）"}
        )

        plt.title('Voice Channel Co-Presence (Hours)', fontsize=16)
        plt.xticks(rotation=45, ha='right', fontsize=tick_fontsize)
        plt.yticks(rotation=0, fontsize=tick_fontsize)
        plt.tight_layout()

        buf = io.BytesIO()
        plt.savefig(buf, format='png', bbox_inches='tight')
        buf.seek(0)
        plt.close()

        return buf, None

    except Exception as e:
        logging.error(f"Error generating heatmap: {e}", exc_info=True)
        return None, f"Error generating heatmap: {str(e)}"


# -------------------------------------------------------------------
# 生成共同在线热图 (相对时长)
# -------------------------------------------------------------------
@bot.command()
@check_channel()
async def show_relationships_relative(ctx):
    """
    命令: !show_relationships_relative
    生成基于"相对关系(%)"的共同语音热图，并发送图片。
    """
    try:
        buf, error = await generate_co_occurrence_heatmap_relative(ctx.guild.id)
        if error:
            await send_to_command_channel(ctx.guild, content=error)
        else:
            file = discord.File(buf, filename='relative_relationships.png')
            await send_to_command_channel(ctx.guild, file=file)
    except Exception as e:
        logging.error(f"Error in show_relationships_relative: {e}", exc_info=True)
        await send_to_command_channel(ctx.guild, content=f"Error generating relative relationship visualization: {str(e)}")

async def generate_co_occurrence_heatmap_relative(guild_id):
    """
    生成显示"共同时长 / 双方最小总时长 (%)"的热图，
    只考虑: 总时长排名前30 + daily排名前20 + 最近加入者，
    并按"总时长"从大到小排序坐标轴。
    """
    try:
        stats = co_occurrence_stats.get(guild_id, {})
        if not stats:
            return None, "No co-occurrence data available."
        
        guild_voice_stat = voice_stats.get(guild_id, {})
        if not guild_voice_stat:
            return None, "No voice_stats data for this guild."

        ranked_by_total = sorted(
            guild_voice_stat.items(),
            key=lambda x: x[1]['total'],
            reverse=True
        )
        top_30_total_ids = [member_id for member_id, _ in ranked_by_total[:30]]

        ranked_by_daily = sorted(
            guild_voice_stat.items(),
            key=lambda x: x[1]['daily'],
            reverse=True
        )
        top_20_daily_ids = [member_id for member_id, _ in ranked_by_daily[:20]]

        guild_obj = bot.get_guild(guild_id)
        if guild_obj is None:
            return None, "Guild not found."
        members_with_joined = [m for m in guild_obj.members if m.joined_at]
        if members_with_joined:
            last_joined_member = max(members_with_joined, key=lambda m: m.joined_at)
            last_joined_id = last_joined_member.id
        else:
            last_joined_id = None

        selected_ids = set(top_30_total_ids + top_20_daily_ids)
        if last_joined_id is not None:
            selected_ids.add(last_joined_id)

        if len(selected_ids) < 2:
            return None, "Not enough users to generate a meaningful heatmap."

        filtered_stats = {}
        for (u1, u2), co_seconds in stats.items():
            if u1 in selected_ids and u2 in selected_ids:
                filtered_stats[(u1, u2)] = co_seconds

        if not filtered_stats:
            return None, "No co-occurrence data available after filtering."

        final_users = set()
        for (u1, u2) in filtered_stats.keys():
            final_users.add(u1)
            final_users.add(u2)
        final_users = list(final_users)

        final_users = sorted(
            final_users,
            key=lambda uid: guild_voice_stat[uid]['total'] if uid in guild_voice_stat else 0,
            reverse=True
        )

        n = len(final_users)
        matrix = np.zeros((n, n), dtype=float)
        idx_map = {uid: i for i, uid in enumerate(final_users)}

        for (u1, u2), co_seconds in filtered_stats.items():
            i = idx_map[u1]
            j = idx_map[u2]
            co_hours = co_seconds / 3600.0
            total_u1_hrs = guild_voice_stat[u1]['total'] / 3600.0 if u1 in guild_voice_stat else 0
            total_u2_hrs = guild_voice_stat[u2]['total'] / 3600.0 if u2 in guild_voice_stat else 0
            denominator = min(total_u1_hrs, total_u2_hrs)
            if denominator <= 0:
                ratio_percent = 0.0
            else:
                ratio_percent = (co_hours / denominator) * 100.0
                if ratio_percent > 100:
                    ratio_percent = 100
            matrix[i][j] = ratio_percent
            matrix[j][i] = ratio_percent

        labels = []
        for uid in final_users:
            m = guild_obj.get_member(uid)
            labels.append(get_preferred_name(m) if m else str(uid))

        count = len(labels)
        fig_width = max(6, min(24, count * 0.8))
        fig_height = max(6, min(24, count * 0.8))

        plt.figure(figsize=(fig_width, fig_height))
        if count <= 10:
            tick_fontsize = 14
        elif count <= 30:
            tick_fontsize = 12
        else:
            tick_fontsize = 8

        sns.heatmap(
            matrix,
            xticklabels=labels,
            yticklabels=labels,
            cmap='flare',
            annot=False,
            vmin=0,
            vmax=100,
            linewidths=.5,
            square=True,
            cbar_kws={"shrink": .8, "label": "共同时长 / 最小总时长 (%)"}
        )

        plt.title('Relative Co-Presence (%)', fontsize=16)
        plt.xticks(rotation=45, ha='right', fontsize=tick_fontsize)
        plt.yticks(rotation=0, fontsize=tick_fontsize)
        plt.tight_layout()

        buf = io.BytesIO()
        plt.savefig(buf, format='png', bbox_inches='tight')
        buf.seek(0)
        plt.close()

        return buf, None
        
    except Exception as e:
        logging.error(f"Error generating relative heatmap: {e}", exc_info=True)
        return None, f"Error generating relative heatmap: {str(e)}"


@bot.command()
@check_channel()
async def stats(ctx, period: str):
    """
    实时查看指定周期的语音统计数据，period 可为 daily, weekly, monthly, yearly
    例如: !stats daily
    """
    valid_periods = ['daily','weekly','monthly','yearly']
    if period not in valid_periods:
        await send_to_command_channel(ctx.guild, content="无效的周期，请输入 daily, weekly, monthly, 或 yearly。")
        return

    await generate_report(period, ctx.guild)

# ---------------- 定期任务 ----------------
@tasks.loop(minutes=5)
async def save_stats():
    await save_voice_stats()
    await save_co_occurrence_stats()

@tasks.loop(hours=1)
async def cleanup_tts_files():
    FILE_RETENTION_SECONDS = 3600
    current_time = time.time()
    tts_files = glob.glob(os.path.join(TTS_CACHE_DIR, "tts_*.mp3"))
    for file in tts_files:
        try:
            file_mtime = os.path.getmtime(file)
            if current_time - file_mtime > FILE_RETENTION_SECONDS:
                os.remove(file)
                logging.info(f"已删除旧的 TTS 文件: {file}")
        except Exception as e:
            logging.error(f"删除 TTS 文件 '{file}' 失败: {e}")

def schedule_reports():
    scheduler.add_job(
        generate_report, 
        CronTrigger(
            hour=0, 
            minute=0, 
            second=0, 
            timezone=timezone('Australia/Sydney')
        ), 
        args=['daily'], 
        id='daily_report'
    )
    
    scheduler.add_job(
        generate_report,
        CronTrigger(
            day_of_week='mon',
            hour=0,
            minute=0,
            second=0,
            timezone=timezone('Australia/Sydney')
        ),
        args=['weekly'],
        id='weekly_report'
    )
    
    scheduler.add_job(
        generate_report,
        CronTrigger(
            day=1,
            hour=0,
            minute=0,
            second=0,
            timezone=timezone('Australia/Sydney')
        ),
        args=['monthly'],
        id='monthly_report'
    )
    
    scheduler.add_job(
        generate_report,
        CronTrigger(
            month=1,
            day=1,
            hour=0,
            minute=0,
            second=0,
            timezone=timezone('Australia/Sydney')
        ),
        args=['yearly'],
        id='yearly_report'
    )
    logging.info("已调度每日、每周、每月和每年的报告生成任务。")

async def generate_report(period, ctx_guild=None):
    """
    生成指定 period 的排行榜报告并发送到文本频道。
    包含:
    1. 文字排行榜
    2. 柱状图可视化
    3. 绝对共同在线时长热图
    4. 相对共同在线时长热图
    """
    guilds = [ctx_guild] if ctx_guild else bot.guilds
    for guild in guilds:
        if guild is None:
            continue
        guild_id = guild.id
        if guild_id not in voice_stats:
            continue
        members_stats = voice_stats[guild_id]
        if not members_stats:
            continue

        sorted_members = sorted(members_stats.items(), key=lambda x: x[1][period], reverse=True)[:10]
        if not sorted_members:
            continue

        report = f"**{period.capitalize()}语音时长排行榜**\n"
        for rank, (member_id, data) in enumerate(sorted_members, start=1):
            member = guild.get_member(member_id)
            if member:
                name = get_preferred_name(member)
                seconds = data[period]
                hours, remainder = divmod(int(seconds), 3600)
                minutes, secs = divmod(remainder, 60)
                report += f"{rank}. {name}: {hours}h {minutes}m {secs}s\n"

        text_channel = guild.system_channel
        if not text_channel or not has_required_permissions(text_channel):
            for channel in guild.text_channels:
                if has_required_permissions(channel):
                    text_channel = channel
                    logging.info(f"使用频道 '{channel.name}' 发送报告。")
                    break

        if text_channel:
            try:
                await send_to_command_channel(guild, content=report)
                logging.info(f"已发送 {period} 文字汇报到服务器 '{guild.name}'。")

                # 2. 生成并发送柱状图
                await generate_periodic_chart(guild, period)
                
                # 3. 绝对时长热图
                buf, error = await generate_co_occurrence_heatmap(guild_id)
                if error:
                    logging.error(f"生成绝对时长热图失败: {error}")
                else:
                    file = discord.File(buf, filename='relationships.png')
                    await send_to_command_channel(guild, content="**共同在线时长热图 (小时)**", file=file)

                # 4. 相对时长热图
                buf, error = await generate_co_occurrence_heatmap_relative(guild_id)
                if error:
                    logging.error(f"生成相对时长热图失败: {error}")
                else:
                    file = discord.File(buf, filename='relative_relationships.png')
                    await send_to_command_channel(guild, content="**共同在线时长热图 (百分比)**", file=file)

                # 如果是自动任务（ctx_guild is None），清零对应周期的统计数据
                if ctx_guild is None:
                    for m_id in voice_stats[guild_id]:
                        voice_stats[guild_id][m_id][period] = 0

            except Exception as e:
                logging.error(f"发送 {period} 完整报告到服务器 '{guild.name}' 失败: {e}")
        else:
            logging.warning(f"在服务器 '{guild.name}' 中未找到可发送消息的文本频道。")

async def generate_periodic_chart(guild, period):
    """生成指定 period 的排行榜柱状图并发送到文本频道。"""
    guild_id = guild.id
    if guild_id not in voice_stats:
        return
    members_stats = voice_stats[guild_id]
    if not members_stats:
        return

    title_map = {
        'daily': '每日语音时长排行榜',
        'weekly': '每周语音时长排行榜',
        'monthly': '每月语音时长排行榜',
        'yearly': '年度语音时长排行榜'
    }
    title = title_map.get(period, f"{period.capitalize()}排行榜")

    sorted_members = sorted(members_stats.items(), key=lambda x: x[1][period], reverse=True)[:10]

    members = []
    durations = []
    for member_id, data in sorted_members:
        member = guild.get_member(member_id)
        if member:
            members.append(get_preferred_name(member))
            durations.append(data[period] / 3600.0)

    if not members:
        logging.info(f"没有成员数据可用于生成 {period} 报告在服务器 '{guild.name}'。")
        return

    if os.path.exists(font_path):
        font_prop = fm.FontProperties(fname=font_path)
        plt.rcParams['font.sans-serif'] = [font_prop.get_name()]
        plt.rcParams['font.family'] = 'sans-serif'
        plt.rcParams['axes.unicode_minus'] = False
        sns.set_theme(style="whitegrid", font=font_prop.get_name(), context="talk")
    else:
        logging.warning(f"字体文件 {font_path} 不存在。将使用默认字体。")
        sns.set_theme(style="whitegrid", context="talk")

    plt.figure(figsize=(12, 8))

    colors = sns.color_palette("Spectral", n_colors=len(durations))
    ax = sns.barplot(x=durations, y=members, palette=colors, edgecolor='black')
    plt.xlabel('语音时长 (小时)', fontsize=14)
    plt.ylabel('成员', fontsize=14)
    plt.title(title, fontsize=16)

    for i, v in enumerate(durations):
        ax.text(
            v + 0.1,
            i,
            f"{v:.1f}h",
            color='black',
            va='center',
            fontsize=10
        )

    plt.tight_layout()

    image_path = os.path.join(TTS_CACHE_DIR, f"{period}_report_{guild_id}.png")
    plt.savefig(image_path, dpi=300, bbox_inches='tight')
    plt.close()

    text_channel = guild.system_channel
    if not text_channel or not has_required_permissions(text_channel):
        for channel in guild.text_channels:
            if has_required_permissions(channel):
                text_channel = channel
                break
    if text_channel:
        try:
            with open(image_path, 'rb') as f:
                picture = discord.File(f)
                await send_to_command_channel(guild, file=picture)
            os.remove(image_path)
            logging.info(f"已发送 {period} 排行榜图片到服务器 '{guild.name}'。")
        except Exception as e:
            logging.error(f"发送 {period} 排行榜图片失败: {e}")
    else:
        logging.warning(f"在服务器 '{guild.name}' 中未找到可发送消息的文本频道。")

class BotException(Exception):
    """Base exception class for bot errors"""
    pass

async def handle_command_error(ctx, error):
    """Global error handler for commands"""
    if isinstance(error, commands.CheckFailure):
        # 如果是因为频道检查失败，直接忽略
        return
    else:
        # 其他错误正常处理
        logging.error(f"Command error in {ctx.command}: {str(error)}")

bot.on_command_error = handle_command_error

@backoff.on_exception(
    backoff.expo,
    (discord.errors.ConnectionClosed, aiohttp.ClientConnectorError),
    max_time=300
)
async def run_bot():
    await bot.start(os.getenv('DISCORD_TOKEN'))

async def health_check(request):
    """Health check endpoint for Docker or service monitoring"""
    return web.Response(text="OK", status=200)

async def start_health_server():
    app = web.Application()
    app.router.add_get('/health', health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', 8080)
    await site.start()

@tasks.loop(minutes=2)
async def check_voice_connections():
    """定期检查所有语音连接的状态"""
    for guild_id, voice_client in list(guild_voice_clients.items()):
        try:
            if not voice_client or not voice_client.is_connected():
                await cleanup_voice_client(guild_id)
                continue
                
            last_success = getattr(voice_client, 'last_success', 0)
            failed_attempts = getattr(voice_client, 'failed_attempts', 0)
            
            # 如果超过5分钟没有成功操作，或失败次数过多，强制重新连接
            if (time.time() - last_success > 300) or (failed_attempts > 3):
                logging.warning(f"检测到可能的语音连接问题，正在重新连接... Guild ID: {guild_id}")
                await cleanup_voice_client(guild_id)
                    
        except Exception as e:
            logging.error(f"检查语音连接状态时发生错误 (Guild ID: {guild_id}): {e}")
            await cleanup_voice_client(guild_id)

# --- 修改 cleanup_voice_client，断开并确保彻底清理 ---
async def cleanup_voice_client(guild_id: int):
    """清理语音客户端的所有状态"""
    try:
        # 获取并清理全局字典中的引用
        voice_client = guild_voice_clients.pop(guild_id, None)
        
        if voice_client:
            try:
                # 强制断开连接
                if voice_client.is_connected():
                    await voice_client.disconnect(force=True)
            except Exception as e:
                logging.error(f"断开语音连接时发生错误: {e}")
            
            # 确保清理所有相关资源
            try:
                voice_client.cleanup()
            except:
                pass

        # 额外检查并清理服务器中的所有语音连接
        guild = bot.get_guild(guild_id)
        if guild:
            for vc in guild.voice_clients:
                try:
                    await vc.disconnect(force=True)
                except:
                    pass

        logging.info("确认已与语音频道断开。")
    except Exception as e:
        logging.error(f"清理语音客户端时发生错误: {e}")

async def send_to_command_channel(guild, content=None, file=None, embed=None):
    """统一的消息发送函数，确保消息发送到命令频道"""
    global command_channel
    try:
        if not command_channel:
            command_channel = guild.get_channel(ALLOWED_COMMAND_CHANNEL_ID)
            if not command_channel:
                logging.error(f"无法找到指定的命令频道 ID: {ALLOWED_COMMAND_CHANNEL_ID}")
                return

        logging.info(f"正在发送消息到命令频道: {command_channel.name}")  # 输出频道名称

        if content:
            await command_channel.send(content)
        if file:
            await command_channel.send(file=file)
        if embed:
            await command_channel.send(embed=embed)
    except Exception as e:
        logging.error(f"发送消息到命令频道时出错: {e}")


async def main():
    try:
        await start_health_server()
        await run_bot()
    except Exception as e:
        logging.error(f"Bot runtime error: {e}", exc_info=True)
    finally:
        await bot.close()

if __name__ == "__main__":
    DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
    if not DISCORD_TOKEN:
        logging.error("未找到 DISCORD_TOKEN 环境变量。请设置后重试。")
        exit(1)
    else:
        try:
            asyncio.run(main())
        except Exception as e:
            logging.error(f"主程序运行时发生异常: {e}")
