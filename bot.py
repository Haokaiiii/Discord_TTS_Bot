# bot.py

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
import matplotlib
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
import warnings
import subprocess

warnings.filterwarnings("ignore", category=UserWarning, module="matplotlib")

DEBOUNCE_TIME = 2.0  # 切换频道的防抖时间（秒）
pending_switch_tasks = {}  # 存储 (guild_id, member_id) -> asyncio.Task

# ============ 加载环境变量 ============
load_dotenv()

# ============ 设置日志输出 ============
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# ============ Matplotlib中文字体配置(尽量避免缺字) ============
matplotlib.use('Agg')  # 在无图形界面的环境中使用 Agg 后端

# 尝试注册常见中文字体文件，如果系统中存在：
possible_font_paths = [
    '/usr/share/fonts/truetype/noto/NotoSansCJKsc-Regular.otf',
    '/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttf',
    '/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf',
    '/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc',
    '/usr/share/fonts/truetype/wqy/wqy-microhei.ttc',
]
for path in possible_font_paths:
    if os.path.exists(path):
        try:
            fm.fontManager.addfont(path)
            logging.info(f"已注册字体文件: {path}")
        except Exception as e:
            logging.warning(f"注册字体失败 {path}: {e}")

# 指定一组「回退」字体，以免某些字符在第一种字体里找不到
plt.rcParams['font.sans-serif'] = [
    'Noto Sans CJK SC',
    'WenQuanYi Zen Hei',
    'WenQuanYi Micro Hei',
]
plt.rcParams['font.family'] = 'sans-serif'
plt.rcParams['axes.unicode_minus'] = False

logging.info("已为 matplotlib 配置中文字体回退列表。")

# Seaborn 风格
sns.set(style="whitegrid")

# ============ 初始化 Discord Bot ============
intents = discord.Intents.default()
intents.voice_states = True
intents.guilds = True
intents.messages = True
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix='!', intents=intents)

# ============ 全局变量与数据结构 ============

guild_voice_clients = {}
guild_tts_queues = {}
guild_tts_tasks = {}

voice_activity = {}       # {guild_id: {member_id: join_time}}
voice_stats = {}          # {guild_id: {member_id: {total, daily, weekly, monthly, yearly}}}
co_occurrence_stats = {}  # {guild_id: {(m1, m2): seconds}}
channel_users = {}        # {guild_id: {channel_id: {member_id: join_time}}}

FFMPEG_EXECUTABLE = "ffmpeg"
TTS_CACHE_DIR = "tts_cache"
os.makedirs(TTS_CACHE_DIR, exist_ok=True)

EXCLUDED_VOICE_CHANNEL_IDS = set()

executor = ThreadPoolExecutor(max_workers=8)

save_lock = Lock()

scheduler = AsyncIOScheduler(timezone=timezone('Australia/Sydney'))

# 添加本地备份路径
BACKUP_DIR = "data_backup"
os.makedirs(BACKUP_DIR, exist_ok=True)

# ============ MongoDB 配置 ============
MONGODB_URI = os.getenv('MONGODB_URI')
if not MONGODB_URI:
    logging.error("未找到 MONGODB_URI 环境变量。请设置后重试。")
    exit(1)

client = MongoClient(MONGODB_URI)
db = client['discord_bot']

voice_stats_col = db['voice_stats']
co_occurrence_col = db['co_occurrence_stats']


# ============ 只响应特定频道命令的全局检查 ============
SPECIFIC_CHANNEL_ID = 555240012460064771  # 你想要监听命令的频道ID

@bot.check
async def check_channel(ctx):
    """
    全局检查器：只有当消息来自指定频道 (SPECIFIC_CHANNEL_ID) 时才能执行命令
    """
    return ctx.channel.id == SPECIFIC_CHANNEL_ID


# ============ 保存/加载函数 ============

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
            
            # 再保存到MongoDB
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
            raise

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
            
            # 再保存到MongoDB
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
            raise

def load_voice_stats():
    """从 MongoDB 或本地备份加载语音统计数据"""
    try:
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

        # 如果无本地备份，则从 MongoDB 加载
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


# ============ Bot事件 ============

@bot.event
async def on_ready():
    logging.info(f'已登录为 {bot.user}')
    logging.info(f"当前已加载命令: {[cmd.name for cmd in bot.commands]}")
    load_voice_stats()
    load_co_occurrence_stats()
    
    # 填充初始化数据
    for guild in bot.guilds:
        try:
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
    logging.info("定期保存数据、报告和清理任务已启动。")


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
    # 最后一次保存数据
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

    # 加入
    if not before.channel and after.channel:
        voice_activity[guild_id][member_id] = now
        ch_id = after.channel.id
        channel_users.setdefault(guild_id, {}).setdefault(ch_id, {})
        channel_users[guild_id][ch_id][member_id] = now
        logging.info(f"用户 {get_preferred_name(member)} 加入了 {after.channel.name} 于 {now}")
        await handle_voice_event(guild, member, before, after, event_type='join')

    # 离开
    elif before.channel and not after.channel:
        join_time = voice_activity[guild_id].pop(member_id, None)
        if join_time:
            duration = (now - join_time).total_seconds()
            for p in ['total', 'daily', 'weekly', 'monthly', 'yearly']:
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

    # 切换
    elif before.channel != after.channel:
        join_time = voice_activity[guild_id].pop(member_id, None)
        if join_time:
            duration = (now - join_time).total_seconds()
            for p in ['total', 'daily', 'weekly', 'monthly', 'yearly']:
                voice_stats[guild_id][member_id][p] += duration

            old_ch_id = before.channel.id
            new_ch_id = after.channel.id
            # 计算旧频道共同在线时长
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

    # 如果仍在语音频道，也更新下
    if after.channel:
        if member_id not in voice_activity[guild_id]:
            voice_activity[guild_id][member_id] = now


@bot.event
async def on_member_update(before, after):
    """如果昵称改变，可以在语音频道中播报 TTS"""
    if before.nick != after.nick:
        guild = after.guild
        guild_id = guild.id
        member_id = after.id
        name_before = get_preferred_name(before)
        name_after = get_preferred_name(after)
        logging.info(f"成员 {name_before} 更改了昵称为 {name_after}")

        voice_client = guild_voice_clients.get(guild_id)
        if voice_client and voice_client.channel:
            if member_id in channel_users.get(guild_id, {}).get(voice_client.channel.id, {}):
                message = f"{name_after} 更改了昵称！"
                await queue_tts(guild, voice_client.channel, message)


# ============ 辅助函数 ============
def get_preferred_name(member):
    """返回成员优先显示名称：先服务器昵称，再 global_name，然后用户名称"""
    if member.nick:
        return member.nick
    elif hasattr(member, 'global_name') and member.global_name:
        return member.global_name
    else:
        return member.name


# ============ 处理用户事件，进行TTS ============

async def handle_voice_event(guild, member, before, after, event_type):
    try:
        # 拉最新的用户信息
        member = await guild.fetch_member(member.id)
        name = get_preferred_name(member)
    except discord.NotFound:
        logging.warning(f"成员未找到或已离开: ID={member.id}")
        return
    except Exception as e:
        logging.error(f"获取成员信息时出错: {e}")
        return

    if event_type == 'switch':
        # 延迟广播
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

    await queue_tts(guild, voice_channel, message)

async def delayed_switch_broadcast(guild, member, voice_channel):
    """切换频道事件后延迟 DEBOUNCE_TIME 秒，有新切换就取消，否则 TTS 播报"""
    try:
        await asyncio.sleep(DEBOUNCE_TIME)
    except asyncio.CancelledError:
        logging.info(f"用户 {get_preferred_name(member)} 的 switch TTS 被新的切换事件取消。")
        return

    message = f"{get_preferred_name(member)} 叛变了！"
    await queue_tts(guild, voice_channel, message)


# ============ TTS 处理队列与 播放逻辑 ============

voice_action_locks = {}  # 每个guild一个Lock，防止并发连接、移动

async def queue_tts(guild, voice_channel, message):
    """将TTS任务放入对应guild的队列"""
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

async def generate_tts(text, output_path):
    """使用 gTTS 生成中文语音"""
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
    """后台协程：持续处理此guild的TTS任务队列"""
    guild = bot.get_guild(guild_id)
    if not guild:
        logging.error(f"无法获取 guild ID {guild_id} 对象。")
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


async def handle_tts_task(task):
    """TTS 播放的核心逻辑，带互斥锁"""
    guild = task['guild']
    voice_channel = task['voice_channel']
    message_content = task['message']
    tts_path = task['tts_path']
    guild_id = guild.id

    if guild_id not in voice_action_locks:
        voice_action_locks[guild_id] = Lock()

    async with voice_action_locks[guild_id]:
        # 找可发送文本的频道
        text_channel = guild.system_channel
        if not text_channel or not has_required_permissions(text_channel):
            for channel in guild.text_channels:
                if has_required_permissions(channel):
                    text_channel = channel
                    break

        if not text_channel:
            logging.warning(f"在服务器 '{guild.name}' 未找到可发消息的文本频道。")
            return

        # 发送文本提示
        try:
            await text_channel.send(message_content, delete_after=5)
            logging.info(f"已发送文本消息: '{message_content}' (5秒后删除)")
        except Exception as e:
            logging.error(f"发送文本消息失败: {e}")
            return

        # 获取/创建 voice_client
        voice_client = guild_voice_clients.get(guild_id)

        # 检查 TTS 文件
        if not os.path.exists(tts_path) or os.path.getsize(tts_path) == 0:
            logging.error(f"TTS 文件无效或为空: {tts_path}")
            return

        # 连接或移动到目标频道
        if not voice_client or not voice_client.is_connected():
            try:
                logging.info("Connecting to voice...")
                voice_client = await voice_channel.connect()
                guild_voice_clients[guild_id] = voice_client
                logging.info(f"已连接到语音频道: '{voice_channel.name}'")
            except discord.ClientException as e:
                if "Already connected to a voice channel" in str(e):
                    logging.error("已经连接到其他语音频道，尝试断开并重连...")
                    try:
                        if voice_client and voice_client.is_connected():
                            logging.info("Disconnected from voice by force... potentially reconnecting.")
                            await voice_client.disconnect()
                            voice_client = None
                        voice_client = await voice_channel.connect()
                        guild_voice_clients[guild_id] = voice_client
                    except Exception as reconnect_err:
                        logging.error(f"强制断开并重连失败: {reconnect_err}")
                        return
                else:
                    logging.error(f"无法连接到语音频道，出现ClientException: {e}")
                    return
            except Exception as e:
                logging.error(f"无法连接到语音频道: {e}")
                return
        else:
            # 已有连接，但频道不同 -> move_to
            if voice_client.channel != voice_channel:
                try:
                    await voice_client.move_to(voice_channel)
                    logging.info(f"已移动到语音频道: '{voice_channel.name}'")
                except discord.ClientException as e:
                    if "Already connected to a voice channel" in str(e):
                        logging.info("Disconnected from voice by force... potentially reconnecting.")
                        try:
                            await voice_client.disconnect()
                            voice_client = None
                            voice_client = await voice_channel.connect()
                            guild_voice_clients[guild_id] = voice_client
                        except Exception as reconnect_err:
                            logging.error(f"重连到语音频道失败: {reconnect_err}")
                            return
                    else:
                        logging.error(f"移动到语音频道失败: {e}")
                        return
                except Exception as move_err:
                    logging.error(f"移动到语音频道时发生错误: {move_err}")
                    return

        # 播放音频
        try:
            audio_source = discord.FFmpegPCMAudio(tts_path, executable=FFMPEG_EXECUTABLE)
            if not voice_client.is_playing():
                voice_client.play(audio_source)
                logging.info(f"开始播放音频: {tts_path}")
            else:
                # 如果正在播放，先等播完
                while voice_client.is_playing():
                    await asyncio.sleep(0.5)
                voice_client.play(audio_source)
                logging.info(f"开始播放音频: {tts_path}")
        except Exception as e:
            logging.error(f"播放音频失败: {e}")
            return

        # 等播放结束
        while voice_client.is_playing():
            await asyncio.sleep(0.1)


def has_required_permissions(channel):
    """检测机器人是否能在此channel发文本和embed"""
    permissions = channel.permissions_for(channel.guild.me)
    return permissions.send_messages and permissions.embed_links


# ============ Bot命令 ============

@bot.command()
async def leave(ctx):
    """让机器人离开当前语音频道"""
    guild = ctx.guild
    voice_client = guild_voice_clients.get(guild.id)
    if voice_client and voice_client.is_connected():
        try:
            await voice_client.disconnect()
            del guild_voice_clients[guild.id]
            await ctx.send("已离开语音频道。", delete_after=5)
        except Exception as e:
            await ctx.send("无法离开语音频道。", delete_after=5)
            logging.error(f"离开语音频道失败: {e}")
    else:
        await ctx.send("机器人当前不在任何语音频道。", delete_after=5)

@bot.command()
async def check_nickname(ctx, member: discord.Member = None):
    """检查指定成员的昵称信息"""
    if member is None:
        member = ctx.author
    name = get_preferred_name(member)
    await ctx.send(f"成员 {member.id} 的首选名称: {name}")
    logging.info(f"检查成员: ID={member.id}, Username={member.name}, Nickname={member.nick}, DisplayName={member.display_name}")

@bot.command()
async def test_delete(ctx):
    """测试消息删除功能"""
    try:
        await ctx.send("这是一条测试消息，将在5秒后删除。", delete_after=5)
    except Exception as e:
        await ctx.send(f"删除失败: {e}", delete_after=5)

@bot.command()
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
                await ctx.send("无法连接到语音频道。")
                return
        else:
            await ctx.send("你当前不在任何语音频道。")
            return

    test_audio_path = os.path.join(TTS_CACHE_DIR, "test.mp3")
    if not os.path.exists(test_audio_path):
        await generate_tts("这是一个测试音频。", test_audio_path)

    try:
        if not os.path.exists(test_audio_path) or os.path.getsize(test_audio_path) == 0:
            logging.error(f"测试音频文件无效或为空: {test_audio_path}")
            await ctx.send("测试音频文件无效。")
            return

        audio_source = discord.FFmpegPCMAudio(test_audio_path, executable=FFMPEG_EXECUTABLE)
        if not voice_client.is_playing():
            voice_client.play(audio_source)
            await ctx.send("正在播放测试音频。", delete_after=5)
        else:
            await ctx.send("语音客户端正在播放其他音频。", delete_after=5)
    except Exception as e:
        logging.error(f"播放测试音频失败: {e}")
        await ctx.send("播放测试音频失败。", delete_after=5)


@bot.command()
async def stats(ctx, period: str):
    """
    查看指定周期(period)的语音统计：daily / weekly / monthly / yearly
    例如: !stats daily
    """
    valid_periods = ['daily','weekly','monthly','yearly']
    if period not in valid_periods:
        await ctx.send("无效周期，请输入: daily, weekly, monthly, 或 yearly。")
        return

    await generate_report(period, ctx.guild)


# ============ 生成共同在线热图(绝对时长) ============
@bot.command()
async def show_relationships(ctx):
    """显示前若干成员的共同在线热图(小时)"""
    try:
        buf, error = await generate_co_occurrence_heatmap(ctx.guild.id)
        if error:
            await ctx.send(error)
            return
        file = discord.File(buf, filename='relationships.png')
        await ctx.send(file=file)
    except Exception as e:
        logging.error(f"Error in show_relationships: {e}", exc_info=True)
        await ctx.send(f"Error generating relationship visualization: {str(e)}")

async def generate_co_occurrence_heatmap(guild_id):
    """
    生成共同在线热图（绝对时长），仅保留：
      - 总时长排名前30
      - daily 排名前20
      - 最近加入者
    最后按总时长排序。图中改用英文或简易文字，尽量避免缺字。
    """
    try:
        stats = co_occurrence_stats.get(guild_id, {})
        if not stats:
            return None, "No co-occurrence data available."
        
        guild_voice_stat = voice_stats.get(guild_id, {})
        if not guild_voice_stat:
            return None, "No voice_stats data."

        # 1) 总时长排名前30
        ranked_by_total = sorted(
            guild_voice_stat.items(),
            key=lambda x: x[1]['total'],
            reverse=True
        )
        top_30_total_ids = [member_id for member_id, _ in ranked_by_total[:30]]

        # 2) daily 前20
        ranked_by_daily = sorted(
            guild_voice_stat.items(),
            key=lambda x: x[1]['daily'],
            reverse=True
        )
        top_20_daily_ids = [member_id for member_id, _ in ranked_by_daily[:20]]

        # 3) 最近加入
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

        # 过滤 stats
        filtered_stats = {}
        for (u1, u2), seconds in stats.items():
            if u1 in selected_ids and u2 in selected_ids:
                filtered_stats[(u1, u2)] = seconds

        if not filtered_stats:
            return None, "No data after filtering."

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
        tick_fontsize = 12 if count <= 30 else 8

        sns.heatmap(
            matrix,
            xticklabels=labels,
            yticklabels=labels,
            cmap='rocket_r',
            annot=False,
            linewidths=.5,
            square=True,
            cbar_kws={"shrink": .8, "label": "Hours Online"}
        )

        plt.title('Voice Channel Hours', fontsize=14)
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
        return None, str(e)


# ============ 生成共同在线热图(相对时长) ============
@bot.command()
async def show_relationships_relative(ctx):
    """生成基于相对占比(%)的共同在线热图"""
    try:
        buf, error = await generate_co_occurrence_heatmap_relative(ctx.guild.id)
        if error:
            await ctx.send(error)
        else:
            file = discord.File(buf, filename='relative_relationships.png')
            await ctx.send(file=file)
    except Exception as e:
        logging.error(f"Error in show_relationships_relative: {e}", exc_info=True)
        await ctx.send(f"Error generating relative relationship visualization: {str(e)}")

async def generate_co_occurrence_heatmap_relative(guild_id):
    """
    生成显示「共同时长 / 双方最小总时长」的百分比(%)，
    并只保留同样的前若干用户与排序逻辑。
    """
    try:
        stats = co_occurrence_stats.get(guild_id, {})
        if not stats:
            return None, "No co-occurrence data."
        
        guild_voice_stat = voice_stats.get(guild_id, {})
        if not guild_voice_stat:
            return None, "No voice_stats data."

        # 前30(总时长) + 前20(daily) + 最近加入
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
            return None, "Not enough users."

        filtered_stats = {}
        for (u1, u2), co_seconds in stats.items():
            if u1 in selected_ids and u2 in selected_ids:
                filtered_stats[(u1, u2)] = co_seconds

        if not filtered_stats:
            return None, "No data after filtering."

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

            total_u1 = guild_voice_stat[u1]['total'] / 3600.0 if u1 in guild_voice_stat else 0
            total_u2 = guild_voice_stat[u2]['total'] / 3600.0 if u2 in guild_voice_stat else 0
            denominator = min(total_u1, total_u2)
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
        tick_fontsize = 12 if count <= 30 else 8

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
            cbar_kws={"shrink": .8, "label": "Co-Presence (%)"}
        )

        plt.title('Relative Co-Presence (%)', fontsize=14)
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
        return None, str(e)


# ============ 定期任务(保存、清理、报告) ============

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
        CronTrigger(hour=0, minute=0, second=0, timezone=timezone('Australia/Sydney')), 
        args=['daily'], 
        id='daily_report'
    )
    scheduler.add_job(
        generate_report,
        CronTrigger(day_of_week='mon', hour=0, minute=0, second=0, timezone=timezone('Australia/Sydney')),
        args=['weekly'],
        id='weekly_report'
    )
    scheduler.add_job(
        generate_report,
        CronTrigger(day=1, hour=0, minute=0, second=0, timezone=timezone('Australia/Sydney')),
        args=['monthly'],
        id='monthly_report'
    )
    scheduler.add_job(
        generate_report,
        CronTrigger(month=1, day=1, hour=0, minute=0, second=0, timezone=timezone('Australia/Sydney')),
        args=['yearly'],
        id='yearly_report'
    )
    logging.info("已调度每日、每周、每月和每年的报告生成任务。")


async def generate_report(period, ctx_guild=None):
    """
    生成并发送指定 period (daily/weekly/monthly/yearly) 的排行榜文本和柱状图。
    如果 ctx_guild 为 None，则对所有 guild 生效；否则仅对指定 guild 发送。
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

        # 排行文本
        report = f"**{period.capitalize()} Ranking (Hours)**\n"
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
                    break

        if text_channel:
            try:
                await text_channel.send(report)
                logging.info(f"已发送 {period} 汇报到服务器 '{guild.name}'。")
                await generate_periodic_chart(guild, period)
                # 自动任务时，发完就清零
                if ctx_guild is None:
                    for member_id in voice_stats[guild_id]:
                        voice_stats[guild_id][member_id][period] = 0
            except Exception as e:
                logging.error(f"发送 {period} 汇报到服务器 '{guild.name}' 失败: {e}")
        else:
            logging.warning(f"在服务器 '{guild.name}' 中未找到可发送消息的文本频道。")


async def generate_periodic_chart(guild, period):
    """生成指定 period 的柱状图并发送"""
    guild_id = guild.id
    if guild_id not in voice_stats:
        return
    members_stats = voice_stats[guild_id]
    if not members_stats:
        return

    title_map = {
        'daily': 'Daily Ranking (Hours)',
        'weekly': 'Weekly Ranking (Hours)',
        'monthly': 'Monthly Ranking (Hours)',
        'yearly': 'Yearly Ranking (Hours)'
    }
    title = title_map.get(period, f"{period.capitalize()} Ranking (Hours)")

    sorted_members = sorted(members_stats.items(), key=lambda x: x[1][period], reverse=True)[:10]

    members = []
    durations = []
    for member_id, data in sorted_members:
        member = guild.get_member(member_id)
        if member:
            members.append(get_preferred_name(member))
            durations.append(data[period] / 3600.0)

    if not members:
        return

    sns.set_theme(style="whitegrid", context="talk")
    plt.figure(figsize=(12, 8))

    colors = sns.color_palette("Spectral", n_colors=len(durations))

    ax = sns.barplot(x=durations, y=members, palette=colors, edgecolor='black')
    plt.xlabel('Hours', fontsize=12)
    plt.ylabel('Members', fontsize=12)
    plt.title(title, fontsize=14)

    # 在柱子上显示数值
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
    plt.savefig(image_path, dpi=300)
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
                await text_channel.send(file=picture)
            os.remove(image_path)
            logging.info(f"已发送 {period} 排行榜图片到服务器 '{guild.name}'。")
        except Exception as e:
            logging.error(f"发送 {period} 排行榜图片失败: {e}")
    else:
        logging.warning(f"在服务器 '{guild.name}' 中未找到可发送消息的文本频道。")


# ============ 命令错误处理 ============

class BotException(Exception):
    pass

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("You don't have permission to use this command.")
    elif isinstance(error, commands.BotMissingPermissions):
        await ctx.send("I don't have the required permissions to execute this command.")
    elif isinstance(error, commands.CommandOnCooldown):
        await ctx.send(f"This command is on cooldown. Try again in {error.retry_after:.2f}s")
    elif isinstance(error, commands.CommandNotFound):
        # 用户输入了不存在的命令
        logging.error(f"Command error in {ctx.command}: {error}", exc_info=True)
        await ctx.send("Unknown command.", delete_after=5)
    else:
        logging.error(f"Command error in {ctx.command}: {error}", exc_info=True)
        await ctx.send("An error occurred while processing your command.")


# ============ 启动Bot ============

@backoff.on_exception(
    backoff.expo,
    (discord.errors.ConnectionClosed, aiohttp.ClientConnectorError),
    max_time=300
)
async def run_bot():
    await bot.start(os.getenv('DISCORD_TOKEN'))

async def health_check(request):
    """健康检查，用于 Docker 容器探针"""
    return web.Response(text="OK", status=200)

async def start_health_server():
    app = web.Application()
    app.router.add_get('/health', health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', 8080)
    await site.start()

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
