import logging
import os
import io
import matplotlib.pyplot as plt
import matplotlib.font_manager as fm
import seaborn as sns
import numpy as np
import pandas as pd
import discord
import networkx as nx
import matplotlib.cm as cm # Import colormap
import matplotlib.colors as mcolors # For distinct colors
from collections import Counter

from utils.config import FONT_PATH
from utils.helpers import get_preferred_name

# Configure Matplotlib font
if os.path.exists(FONT_PATH):
    font_prop = fm.FontProperties(fname=FONT_PATH)
    plt.rcParams['font.sans-serif'] = [font_prop.get_name()]
    plt.rcParams['font.family'] = 'sans-serif'
    plt.rcParams['axes.unicode_minus'] = False
    # Apply font prop directly to sns.set_theme if using newer Seaborn versions
    try:
        sns.set_theme(style="whitegrid", font=font_prop.get_name())
    except TypeError:
        # Fallback for older versions or if direct font name setting fails
        sns.set_theme(style="whitegrid")
        plt.rcParams['font.sans-serif'] = [font_prop.get_name()]
        logging.warning("Could not set font directly in sns.set_theme, using plt.rcParams fallback.")

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
        io.BytesIO | None: A BytesIO object containing the PNG image data, or None on error.
    """
    if data.empty:
        logging.warning(f"Attempted to generate heatmap '{title}' with empty data.")
        return None

    # Adjust figure size dynamically
    figsize_x = max(10, len(data.columns) * 0.8)
    figsize_y = max(8, len(data.index) * 0.6)
    plt.figure(figsize=(figsize_x, figsize_y))
    
    try:
        sns.heatmap(data, annot=annot, fmt=fmt, cmap=color_map, linewidths=.5, square=False)
        plt.title(title)
        plt.xticks(rotation=45, ha='right') # Improve label readability
        plt.yticks(rotation=0)
        plt.tight_layout(pad=2.0) # Add padding

        buf = io.BytesIO()
        plt.savefig(buf, format='png', dpi=150, bbox_inches='tight')
        buf.seek(0)
        return buf
    except Exception as e:
        logging.error(f"Error generating heatmap '{title}': {e}", exc_info=True)
        return None
    finally:
        plt.close() # Close the plot to free memory

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

    # Fetch members efficiently once
    try:
        await guild.chunk() # Ensure member cache is populated if needed and intents allow
        members_map = {m.id: m for m in guild.members}
    except discord.errors.ClientException:
        logging.warning(f"Could not chunk guild {guild.id}, member names might be incomplete.")
        # Fallback: Use cached members, might miss some users
        members_map = {m.id: m for m in guild.members}
        if not members_map:
             logging.error(f"No members found in cache for guild {guild.id}. Cannot generate heatmap.")
             return None

    member_ids_with_data = set()
    for m1, m2 in co_occurrence_data.keys():
        member_ids_with_data.add(m1)
        member_ids_with_data.add(m2)

    active_member_ids = sorted([mid for mid in member_ids_with_data if mid in members_map])
    member_names = {mid: get_preferred_name(members_map[mid]) for mid in active_member_ids}

    matrix_size = len(active_member_ids)
    if matrix_size < 2:
        logging.info(f"Not enough active members ({matrix_size}) with co-occurrence data in guild {guild.id}.")
        return None

    matrix = np.zeros((matrix_size, matrix_size))
    total_times = defaultdict(float) # Needed for relative calculation

    # Populate matrix and total times
    for i, m1_id in enumerate(active_member_ids):
        for j, m2_id in enumerate(active_member_ids):
            if i == j:
                continue # Skip self-co-occurrence
            pair = tuple(sorted((m1_id, m2_id)))
            duration_seconds = co_occurrence_data.get(pair, 0.0)
            matrix[i, j] = duration_seconds / 3600.0 # Convert to hours
            # Accumulate total time for relative calculation (only need to do once per pair)
            if i < j and duration_seconds > 0:
                total_times[m1_id] += duration_seconds
                total_times[m2_id] += duration_seconds

    if relative:
        relative_matrix = np.zeros((matrix_size, matrix_size))
        for i, m1_id in enumerate(active_member_ids):
            m1_total = total_times.get(m1_id, 0.0)
            for j, m2_id in enumerate(active_member_ids):
                if i == j or m1_total == 0:
                    continue # Avoid division by zero
                pair = tuple(sorted((m1_id, m2_id)))
                duration_seconds = co_occurrence_data.get(pair, 0.0)
                relative_matrix[i, j] = (duration_seconds / m1_total) * 100
        matrix = relative_matrix
        title = f'{guild.name} 成员共同在线时间比例 (%)'
        fmt = ".1f"
        color_map = "plasma"
    else:
        title = f'{guild.name} 成员共同在线时长 (小时)'
        fmt = ".1f"
        color_map = "rocket_r"

    active_member_names_list = [member_names[mid] for mid in active_member_ids]
    df = pd.DataFrame(matrix, index=active_member_names_list, columns=active_member_names_list)

    # Heatmap function now handles empty check, no need to filter here
    # df = df.loc[df.sum(axis=1) > 0, df.sum(axis=0) > 0]
    # if df.empty:
    #     logging.info(f"Filtered co-occurrence data is empty for guild {guild.id}. No heatmap generated.")
    #     return None

    return create_heatmap(df, title, color_map=color_map, fmt=fmt, annot=True if len(df) <= 20 else False)

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
    # Fetch members efficiently
    try:
        await guild.chunk() # Ensure member cache is populated if needed
        members_map = {m.id: m for m in guild.members}
    except discord.errors.ClientException:
        logging.warning(f"Could not chunk guild {guild.id}, member names might be incomplete for chart.")
        members_map = {m.id: m for m in guild.members}

    for member_id, stats in voice_stats_data.items():
        member = members_map.get(member_id)
        name = get_preferred_name(member) if member else f"Left User ({member_id})"
        duration_seconds = stats.get(period, 0)
        if duration_seconds > 1: # Only include members with >1s activity in the period
            data.append({'Member': name, 'DurationHours': duration_seconds / 3600.0})

    if not data:
        logging.info(f"No significant voice activity found for period '{period}' in guild {guild.id}. No chart generated.")
        return None

    df = pd.DataFrame(data)
    df = df.sort_values(by='DurationHours', ascending=False).head(30) # Limit to top 30

    plt.figure(figsize=(12, max(6, len(df) * 0.4)))
    try:
        barplot = sns.barplot(x='DurationHours', y='Member', data=df, palette="viridis", orient='h')

        # Add labels to bars
        for container in barplot.containers:
            barplot.bar_label(container, fmt='%.1f h', padding=3, fontsize=10)

        plt.title(f'{guild.name} {title_period}语音在线时长 (Top {len(df)})')
        plt.xlabel('时长 (小时)')
        plt.ylabel('成员')
        plt.tight_layout(pad=1.5)

        buf = io.BytesIO()
        plt.savefig(buf, format='png', dpi=150, bbox_inches='tight')
        buf.seek(0)
        return buf
    except Exception as e:
        logging.error(f"Error generating {period} chart for guild {guild.id}: {e}", exc_info=True)
        return None
    finally:
        plt.close()

async def generate_relationship_network_graph(guild: discord.Guild, co_occurrence_data: dict, weekly_stats: dict) -> io.BytesIO | None:
    """Generates a network graph visualizing co-occurrence relationships 
       for top 10 by co-occurrence and top 10 by weekly activity.

    Args:
        guild (discord.Guild): The guild for which to generate the graph.
        co_occurrence_data (dict): Dictionary containing co-occurrence duration in seconds {(m1_id, m2_id): seconds}.
        weekly_stats (dict): Dictionary containing weekly voice duration for users {member_id: seconds}.

    Returns:
        io.BytesIO | None: A BytesIO object containing the PNG image data, or None if error/no data.
    """
    if not co_occurrence_data:
        logging.info(f"No co-occurrence data for guild {guild.id} to generate network graph.")
        return None

    # --- Node Selection --- 
    # Calculate total co-occurrence time per user
    total_co_occurrence_per_user = Counter()
    valid_pairs = set()
    for (m1_id, m2_id), duration_seconds in co_occurrence_data.items():
        if duration_seconds >= 60: # Only consider pairs with >= 1 min co-occurrence
             total_co_occurrence_per_user[m1_id] += duration_seconds
             total_co_occurrence_per_user[m2_id] += duration_seconds
             valid_pairs.add(tuple(sorted((m1_id, m2_id))))

    # Get top 10 by total co-occurrence
    top_co_occurrence_users = {uid for uid, _ in total_co_occurrence_per_user.most_common(10)}
    logging.debug(f"Top 10 Co-occurrence Users (IDs): {top_co_occurrence_users}")

    # Get top 10 by weekly activity (ensure weekly_stats is not None)
    if weekly_stats is None: weekly_stats = {}
    top_weekly_active_users = {uid for uid, _ in sorted(weekly_stats.items(), key=lambda item: item[1], reverse=True)[:10]}
    logging.debug(f"Top 10 Weekly Active Users (IDs): {top_weekly_active_users}")

    # Combine the sets and ensure minimum node count
    selected_user_ids = top_co_occurrence_users.union(top_weekly_active_users)

    if len(selected_user_ids) < 2:
        logging.info(f"Not enough users selected ({len(selected_user_ids)}) based on criteria for guild {guild.id}. No graph generated.")
        return None
    logging.info(f"Selected {len(selected_user_ids)} users for relationship graph in guild {guild.id}. IDs: {selected_user_ids}")

    # --- Build Subgraph --- 
    try:
        await guild.chunk() # Ensure member cache is populated
        members_map = {m.id: m for m in guild.members}
    except discord.errors.ClientException:
        logging.warning(f"Could not chunk guild {guild.id}, member names might be incomplete for network graph.")
        members_map = {m.id: m for m in guild.members}

    G = nx.Graph()
    edges_data = []
    min_duration = float('inf')
    max_duration = 0.0

    # Add nodes for selected users
    nodes_added = set()
    for user_id in selected_user_ids:
        member = members_map.get(user_id)
        if member:
            name = get_preferred_name(member)
            G.add_node(user_id, label=name)
            nodes_added.add(user_id)
        else:
             logging.warning(f"Could not find member info for selected user ID {user_id} in guild {guild.id}. Skipping node.")
    
    if G.number_of_nodes() < 2: # Check again after potential member fetch failures
        logging.info(f"Not enough valid nodes ({G.number_of_nodes()}) after fetching member info for guild {guild.id}. No graph generated.")
        return None

    # Add edges between selected users if they have valid co-occurrence
    for m1_id in nodes_added:
        for m2_id in nodes_added:
            if m1_id >= m2_id: continue # Avoid self-loops and duplicate pairs
            pair = (m1_id, m2_id) # Already sorted because nodes_added is iterated in order? Ensure anyway.
            if pair in valid_pairs:
                 duration_seconds = co_occurrence_data.get(pair, 0.0) # Should exist if in valid_pairs
                 if duration_seconds > 0: # Should be true if >= 60s filter passed
                     G.add_edge(m1_id, m2_id, weight=duration_seconds)
                     edges_data.append(duration_seconds)
                     min_duration = min(min_duration, duration_seconds)
                     max_duration = max(max_duration, duration_seconds)
    
    if G.number_of_edges() == 0:
         logging.info(f"Selected users for guild {guild.id} have no co-occurrence edges between them. No graph generated.")
         # Optionally, draw just the nodes? For now, return None.
         return None

    # --- Graph Drawing --- 
    plt.figure(figsize=(20, 20)) # Adjusted size back slightly
    try:
        # Layout algorithm - spring layout with strong separation
        node_count = G.number_of_nodes()
        k_value = 4.0 / np.sqrt(node_count) if node_count > 0 else 1.0 # Keep strong separation
        pos = nx.spring_layout(G, k=k_value, iterations=200, seed=42)
        
        # Assign unique colors to nodes
        colors = plt.get_cmap('tab20').colors # Colormap with 20 distinct colors
        node_color_map = {node: colors[i % len(colors)] for i, node in enumerate(G.nodes())}
        node_colors = [node_color_map[node] for node in G.nodes()]

        # Node size based on degree within the *subgraph*
        degrees = [G.degree(n) for n in G.nodes()]
        min_degree, max_degree = (min(degrees), max(degrees)) if degrees else (1, 1)
        node_sizes = [600 + (d - min_degree) / max(1, max_degree - min_degree) * 2000 for d in degrees]

        # Edge width/alpha scaled more aggressively for emphasis
        if max_duration <= min_duration: # Avoid division by zero/invalid range
             norm_weights = [0.5] * G.number_of_edges()
        else:
             # Scale weights logarithmically? Or just power scale?
             # Power scale: Emphasizes higher values more
             power = 1.5 
             norm_weights = [((d['weight'] - min_duration) / (max_duration - min_duration)) ** power 
                           for u, v, d in G.edges(data=True)]
        
        # Adjust width/alpha based on normalized weights
        edge_widths = [0.5 + w * 5.0 for w in norm_weights] # Thinner base, larger max
        edge_alphas = [0.1 + w * 0.7 for w in norm_weights] # Fainter base, stronger max

        # Draw nodes with unique colors
        nx.draw_networkx_nodes(G, pos, node_size=node_sizes, node_color=node_colors, alpha=0.9)

        # Draw edges
        nx.draw_networkx_edges(G, pos, width=edge_widths, alpha=edge_alphas, edge_color='darkgrey') # Darker edges

        # Draw labels
        labels = nx.get_node_attributes(G, 'label')
        nx.draw_networkx_labels(G, pos, labels=labels, font_size=9, 
                               font_family=font_prop.get_name() if font_prop else 'sans-serif',
                               bbox=dict(facecolor='white', alpha=0.6, edgecolor='none', boxstyle='round,pad=0.2'))

        plt.title(f'{guild.name} - 成员关系网络图 (Top 10 Co-Occur + Top 10 Weekly)', fontsize=16, fontproperties=font_prop if font_prop else None)
        plt.axis('off')

        buf = io.BytesIO()
        plt.savefig(buf, format='png', dpi=180, bbox_inches='tight') 
        buf.seek(0)
        return buf

    except Exception as e:
        logging.error(f"Error generating relationship network graph for guild {guild.id}: {e}", exc_info=True)
        return None
    finally:
        plt.close() 