import logging
import os
import io
import matplotlib.pyplot as plt
import matplotlib.font_manager as fm
import seaborn as sns
import numpy as np
import pandas as pd
import discord

from utils.config import FONT_PATH
from utils.helpers import get_preferred_name

# Configure Matplotlib font
if os.path.exists(FONT_PATH):
    font_prop = fm.FontProperties(fname=FONT_PATH)
    plt.rcParams['font.sans-serif'] = [font_prop.get_name()]
    plt.rcParams['font.family'] = 'sans-serif'
    plt.rcParams['axes.unicode_minus'] = False
    sns.set_theme(style="whitegrid", font=font_prop.get_name())
    logging.info(f"Using font: {font_prop.get_name()} from {FONT_PATH}")
else:
    logging.warning(f"Font file {FONT_PATH} not found. Using default font.")
    sns.set_theme(style="whitegrid")

def create_heatmap(data: pd.DataFrame, title: str, color_map="viridis", annot=True, fmt=".1f") -> io.BytesIO:
    """Generates a heatmap from a Pandas DataFrame and returns it as BytesIO.

    Args:
        data (pd.DataFrame): The data to plot.
        title (str): The title for the plot.
        color_map (str): The colormap for the heatmap.
        annot (bool): Whether to annotate the cells.
        fmt (str): String formatting code to use when adding annotations.

    Returns:
        io.BytesIO: A BytesIO object containing the PNG image data.
    """
    if data.empty:
        logging.warning(f"Attempted to generate heatmap '{title}' with empty data.")
        return None

    plt.figure(figsize=(max(10, len(data.columns) * 0.8), max(8, len(data.index) * 0.6)))
    sns.heatmap(data, annot=annot, fmt=fmt, cmap=color_map, linewidths=.5)
    plt.title(title)
    plt.tight_layout()

    buf = io.BytesIO()
    try:
        plt.savefig(buf, format='png', dpi=150)
        buf.seek(0)
    except Exception as e:
        logging.error(f"Error saving heatmap '{title}' to buffer: {e}", exc_info=True)
        buf.close()
        return None
    finally:
        plt.close() # Close the plot to free memory

    return buf

async def generate_co_occurrence_heatmap(guild: discord.Guild, co_occurrence_data: dict, relative: bool = False) -> io.BytesIO | None:
    """Generates a co-occurrence heatmap (absolute or relative) for a guild.

    Args:
        guild (discord.Guild): The guild for which to generate the heatmap.
        co_occurrence_data (dict): Dictionary containing co-occurrence duration in seconds {(m1_id, m2_id): seconds}.
        relative (bool): If True, calculates relative co-occurrence time.

    Returns:
        io.BytesIO | None: A BytesIO object containing the PNG image data, or None if error/no data.
    """
    if not co_occurrence_data:
        logging.info(f"No co-occurrence data for guild {guild.id} to generate heatmap.")
        return None

    members = {m.id: get_preferred_name(m) for m in guild.members} # Get current members for names
    member_ids = sorted(list(members.keys()))
    matrix_size = len(member_ids)

    if matrix_size < 2:
        logging.info(f"Not enough members ({matrix_size}) in guild {guild.id} to generate co-occurrence heatmap.")
        return None

    matrix = np.zeros((matrix_size, matrix_size))
    total_times = {mid: 0 for mid in member_ids} # Needed for relative calculation

    # Populate matrix and total times
    for i, m1_id in enumerate(member_ids):
        for j, m2_id in enumerate(member_ids):
            if i == j:
                continue # Skip self-co-occurrence
            pair = tuple(sorted((m1_id, m2_id)))
            duration_seconds = co_occurrence_data.get(pair, 0)
            matrix[i, j] = duration_seconds / 3600.0 # Convert to hours
            # Accumulate total time for relative calculation (only need to do once per pair)
            if i < j:
                total_times[m1_id] += duration_seconds
                total_times[m2_id] += duration_seconds

    if relative:
        relative_matrix = np.zeros((matrix_size, matrix_size))
        for i, m1_id in enumerate(member_ids):
            for j, m2_id in enumerate(member_ids):
                if i == j or total_times[m1_id] == 0:
                    continue # Avoid division by zero
                pair = tuple(sorted((m1_id, m2_id)))
                duration_seconds = co_occurrence_data.get(pair, 0)
                relative_matrix[i, j] = (duration_seconds / total_times[m1_id]) * 100
        matrix = relative_matrix
        title = f'{guild.name} 成员共同在线时间比例 (%)'
        fmt = ".1f"
    else:
        title = f'{guild.name} 成员共同在线时长 (小时)'
        fmt = ".1f"

    member_names = [members.get(mid, f"Unknown ({mid})") for mid in member_ids]
    df = pd.DataFrame(matrix, index=member_names, columns=member_names)

    # Filter out members with no interaction time at all (rows/cols sum to 0)
    df = df.loc[df.sum(axis=1) > 0, df.sum(axis=0) > 0]

    if df.empty:
        logging.info(f"Filtered co-occurrence data is empty for guild {guild.id}. No heatmap generated.")
        return None

    return create_heatmap(df, title, color_map="rocket_r", fmt=fmt, annot=True if len(df) <= 20 else False)

async def generate_periodic_chart(guild: discord.Guild, voice_stats_data: dict, period: str) -> io.BytesIO | None:
    """Generates a bar chart for voice activity over a specific period.

    Args:
        guild (discord.Guild): The guild object.
        voice_stats_data (dict): The voice statistics data for the guild {member_id: {period: seconds}}.
        period (str): The period key (e.g., 'daily', 'weekly', 'monthly', 'total').

    Returns:
        io.BytesIO | None: A BytesIO object containing the PNG image data, or None if error/no data.
    """
    period_map = {
        'daily': '今日', 'weekly': '本周', 'monthly': '本月',
        'yearly': '今年', 'total': '总计'
    }
    title_period = period_map.get(period, period.capitalize())

    if not voice_stats_data:
        logging.info(f"No voice stats data for guild {guild.id} to generate {period} chart.")
        return None

    data = []
    member_names = {}
    for member_id, stats in voice_stats_data.items():
        member = guild.get_member(member_id)
        name = get_preferred_name(member) if member else f"Left User ({member_id})"
        member_names[member_id] = name
        duration_seconds = stats.get(period, 0)
        if duration_seconds > 0: # Only include members with activity in the period
            data.append({'Member': name, 'DurationHours': duration_seconds / 3600.0})

    if not data:
        logging.info(f"No voice activity found for period '{period}' in guild {guild.id}. No chart generated.")
        return None

    df = pd.DataFrame(data)
    df = df.sort_values(by='DurationHours', ascending=False)

    plt.figure(figsize=(12, max(6, len(df) * 0.4)))
    barplot = sns.barplot(x='DurationHours', y='Member', data=df, palette="viridis")

    # Add labels to bars
    for container in barplot.containers:
        barplot.bar_label(container, fmt='%.1f h', padding=3)

    plt.title(f'{guild.name} {title_period}语音在线时长')
    plt.xlabel('时长 (小时)')
    plt.ylabel('成员')
    plt.tight_layout()

    buf = io.BytesIO()
    try:
        plt.savefig(buf, format='png', dpi=150)
        buf.seek(0)
    except Exception as e:
        logging.error(f"Error saving {period} chart for guild {guild.id} to buffer: {e}", exc_info=True)
        buf.close()
        return None
    finally:
        plt.close()

    return buf 