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
from collections import Counter, defaultdict
import warnings

from utils.config import FONT_PATH
from utils.helpers import get_preferred_name

# Configure Matplotlib font with better error handling
plt.rcParams['font.sans-serif'] = ['WenQuanYi Zen Hei', 'DejaVu Sans', 'Arial Unicode MS', 'sans-serif']
plt.rcParams['axes.unicode_minus'] = False

# Try to load Chinese font if available, but provide fallback
font_loaded = False
if os.path.exists(FONT_PATH):
    try:
        font_prop = fm.FontProperties(fname=FONT_PATH)
        plt.rcParams['font.sans-serif'] = [font_prop.get_name()] + plt.rcParams['font.sans-serif']
        # Apply font prop directly to sns.set_theme if using newer Seaborn versions
        try:
            sns.set_theme(style="whitegrid", font=font_prop.get_name())
        except TypeError:
            # Fallback for older versions or if direct font name setting fails
            sns.set_theme(style="whitegrid")
            plt.rcParams['font.sans-serif'] = [font_prop.get_name()] + plt.rcParams['font.sans-serif']
            logging.warning("Could not set font directly in sns.set_theme, using plt.rcParams fallback.")

        logging.info(f"Using font: {font_prop.get_name()} from {FONT_PATH}")
        font_loaded = True
    except Exception as e:
        logging.warning(f"Error loading font from {FONT_PATH}: {e}")
        
if not font_loaded:
    logging.warning(f"Font file {FONT_PATH} not found or couldn't be loaded. Using default fonts.")
    sns.set_theme(style="whitegrid")
    
# Configure matplotlib to not raise warnings for missing glyphs
warnings.filterwarnings("ignore", category=UserWarning, module="matplotlib")

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
    
    logging.debug(f"[Create Heatmap - {title}] Starting plot generation...")
    try:
        # Use plt.figure within the try block
        logging.debug(f"[Create Heatmap - {title}] Creating figure...")
        plt.figure(figsize=(figsize_x, figsize_y))
        logging.debug(f"[Create Heatmap - {title}] Figure created.")
        
        # Suppress warnings specifically during seaborn plotting
        logging.debug(f"[Create Heatmap - {title}] Calling sns.heatmap...")
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", category=UserWarning)
            sns.heatmap(data, annot=annot, fmt=fmt, cmap=color_map, linewidths=.5, square=False)
        logging.debug(f"[Create Heatmap - {title}] sns.heatmap call finished.")
            
        logging.debug(f"[Create Heatmap - {title}] Setting title and ticks...")
        plt.title(title)
        plt.xticks(rotation=45, ha='right') # Improve label readability
        plt.yticks(rotation=0)
        logging.debug(f"[Create Heatmap - {title}] Title and ticks set.")
        
        # Safely apply tight_layout with fallback
        logging.debug(f"[Create Heatmap - {title}] Attempting tight_layout...")
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                plt.tight_layout(pad=2.0)  # Add padding
            logging.debug(f"[Create Heatmap - {title}] tight_layout succeeded.")
        except Exception as layout_error:
            logging.warning(f"Error during tight_layout for heatmap '{title}': {layout_error}")
            plt.subplots_adjust(left=0.1, right=0.9, top=0.9, bottom=0.1)
            logging.debug(f"[Create Heatmap - {title}] tight_layout failed, applied subplots_adjust.")

        buf = io.BytesIO()
        logging.debug(f"[Create Heatmap - {title}] Attempting to save figure to buffer...")
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                plt.savefig(buf, format='png', dpi=150, bbox_inches='tight')
            logging.debug(f"[Create Heatmap - {title}] Saved figure with bbox_inches='tight'.")
        except Exception as save_error:
            logging.warning(f"Error saving heatmap '{title}' with bbox_inches='tight', trying without: {save_error}")
            try:
                 with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    plt.savefig(buf, format='png', dpi=150)
                 logging.debug(f"[Create Heatmap - {title}] Saved figure without bbox_inches='tight'.")
            except Exception as e:
                logging.error(f"All attempts to save heatmap '{title}' failed: {e}")
                logging.debug(f"[Create Heatmap - {title}] All save attempts failed.")
                return None
            
        buf.seek(0)
        logging.debug(f"[Create Heatmap - {title}] Figure saved to buffer successfully.")
        return buf
    except Exception as e:
        logging.error(f"Error generating heatmap '{title}': {e}", exc_info=True)
        logging.debug(f"[Create Heatmap - {title}] Caught exception during generation.")
        return None
    finally:
        logging.debug(f"[Create Heatmap - {title}] Closing plot figure.")
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
    logging.info(f"[Generate Heatmap] Starting for guild {guild.id} (Relative: {relative})")
    if not co_occurrence_data or not isinstance(co_occurrence_data, dict):
        logging.warning(f"[Generate Heatmap] Invalid or empty co_occurrence_data provided for guild {guild.id}. Type: {type(co_occurrence_data)}")
        return None

    # --- Defensive Data Extraction --- 
    logging.debug("[Generate Heatmap] Starting defensive data extraction...")
    processed_pairs = []
    member_ids_with_data = set()
    total_times = defaultdict(float) # For relative calculation

    for key, duration in co_occurrence_data.items():
        # Minimal logging inside the loop to avoid spam, focus on warnings/errors
        if not isinstance(key, tuple) or len(key) != 2:
            logging.warning(f"[Generate Heatmap] Skipping invalid key in co_occurrence_data: {key} (type: {type(key)}) ")
            continue
        try:
            m1_id, m2_id = int(key[0]), int(key[1])
            duration_float = float(duration)
            if duration_float <= 0:
                continue # Skip zero or negative durations
                
            processed_pairs.append(((m1_id, m2_id), duration_float))
            member_ids_with_data.add(m1_id)
            member_ids_with_data.add(m2_id)
            total_times[m1_id] += duration_float
            total_times[m2_id] += duration_float
            
        except (ValueError, TypeError) as e:
            logging.warning(f"[Generate Heatmap] Skipping invalid data entry: Key={key}, Duration={duration}. Error: {e}")
            continue
            
    if not processed_pairs:
        logging.info(f"[Generate Heatmap] No valid co-occurrence pairs found after processing for guild {guild.id}.")
        return None
    logging.info(f"[Generate Heatmap] Defensive data extraction complete. Found {len(processed_pairs)} valid pairs involving {len(member_ids_with_data)} unique member IDs.")
    # --- End Defensive Data Extraction ---

    # Fetch members efficiently once
    logging.debug("[Generate Heatmap] Fetching guild members...")
    try:
        await guild.chunk()
        members_map = {m.id: m for m in guild.members}
        logging.debug(f"[Generate Heatmap] Fetched {len(members_map)} members.")
    except Exception as e:
        logging.warning(f"[Generate Heatmap] Error while chunking guild {guild.id} for heatmap: {e}")
        members_map = {m.id: m for m in guild.members}
        if not members_map:
             logging.error(f"[Generate Heatmap] No members found in cache for guild {guild.id}. Cannot generate heatmap.")
             return None
        logging.debug(f"[Generate Heatmap] Using {len(members_map)} cached members.")

    active_member_ids = sorted([mid for mid in member_ids_with_data if mid in members_map])
    member_names = {mid: get_preferred_name(members_map[mid]) for mid in active_member_ids}
    logging.debug(f"[Generate Heatmap] Found {len(active_member_ids)} active members with data.")

    matrix_size = len(active_member_ids)
    if matrix_size < 2:
        logging.info(f"[Generate Heatmap] Not enough active members ({matrix_size}) with co-occurrence data in guild {guild.id}.")
        return None

    logging.debug(f"[Generate Heatmap] Creating matrix of size {matrix_size}x{matrix_size}...")
    matrix = np.zeros((matrix_size, matrix_size))

    # Create a lookup for the processed pairs
    processed_pairs_dict = {tuple(sorted(pair)): dur for pair, dur in processed_pairs}

    # Populate matrix using active_member_ids index
    logging.debug("[Generate Heatmap] Populating matrix...")
    for i, m1_id in enumerate(active_member_ids):
        for j, m2_id in enumerate(active_member_ids):
            if i == j:
                continue 
            pair_key = tuple(sorted((m1_id, m2_id)))
            duration_seconds = processed_pairs_dict.get(pair_key, 0.0)
            matrix[i, j] = duration_seconds / 3600.0 # Convert to hours
    logging.debug("[Generate Heatmap] Matrix populated (absolute hours).")

    if relative:
        logging.debug("[Generate Heatmap] Calculating relative matrix...")
        relative_matrix = np.zeros((matrix_size, matrix_size))
        for i, m1_id in enumerate(active_member_ids):
            m1_total = total_times.get(m1_id, 0.0) 
            for j, m2_id in enumerate(active_member_ids):
                if i == j or m1_total == 0:
                    continue
                pair_key = tuple(sorted((m1_id, m2_id)))
                duration_seconds = processed_pairs_dict.get(pair_key, 0.0)
                relative_matrix[i, j] = (duration_seconds / m1_total) * 100 if m1_total > 0 else 0
                
        matrix = relative_matrix
        title = f'{guild.name} 成员共同在线时间比例 (%)'
        fmt = ".1f"
        color_map = "plasma"
        logging.debug("[Generate Heatmap] Relative matrix calculation complete.")
    else:
        title = f'{guild.name} 成员共同在线时长 (小时)'
        fmt = ".1f"
        color_map = "rocket_r"

    active_member_names_list = [member_names[mid] for mid in active_member_ids]
    logging.debug("[Generate Heatmap] Creating Pandas DataFrame...")
    df = pd.DataFrame(matrix, index=active_member_names_list, columns=active_member_names_list)
    logging.debug("[Generate Heatmap] DataFrame created. Shape: {}".format(df.shape))

    # Heatmap function now handles empty check
    logging.info("[Generate Heatmap] Calling create_heatmap function...")
    heatmap_result = create_heatmap(df, title, color_map=color_map, fmt=fmt, annot=True if len(df) <= 20 else False)
    logging.info("[Generate Heatmap] create_heatmap call finished.")
    return heatmap_result

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
        await guild.chunk()
        members_map = {m.id: m for m in guild.members}
    except Exception as e:
        logging.warning(f"Error while chunking guild {guild.id} for {period} chart: {e}")
        members_map = {m.id: m for m in guild.members}

    for member_id, stats in voice_stats_data.items():
        member = members_map.get(member_id)
        name = get_preferred_name(member) if member else f"Left User ({member_id})"
        duration_seconds = stats.get(period, 0)
        if duration_seconds > 1:
            data.append({'Member': name, 'DurationHours': duration_seconds / 3600.0})

    if not data:
        logging.info(f"No significant voice activity found for period '{period}' in guild {guild.id}. No chart generated.")
        return None

    df = pd.DataFrame(data)
    df = df.sort_values(by='DurationHours', ascending=False).head(30)

    try:
        plt.figure(figsize=(12, max(6, len(df) * 0.4)))
        
        # Suppress warnings specifically during seaborn plotting and labeling
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", category=UserWarning)
            barplot = sns.barplot(x='DurationHours', y='Member', hue='Member', data=df, palette="viridis", orient='h', legend=False)
            # Add labels to bars
            for container in barplot.containers:
                barplot.bar_label(container, fmt='%.1f h', padding=3, fontsize=10)

        plt.title(f'{guild.name} {title_period}语音在线时长 (Top {len(df)})')
        plt.xlabel('时长 (小时)')
        plt.ylabel('成员')
        
        # Safely apply tight_layout with fallback
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                plt.tight_layout(pad=1.5)
        except Exception as layout_error:
            logging.warning(f"Error during tight_layout for {period} chart: {layout_error}")
            plt.subplots_adjust(left=0.2, right=0.9, top=0.9, bottom=0.1)

        buf = io.BytesIO()
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                plt.savefig(buf, format='png', dpi=150, bbox_inches='tight')
        except Exception as save_error:
            logging.warning(f"Error saving {period} chart with bbox_inches='tight', trying without: {save_error}")
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    plt.savefig(buf, format='png', dpi=150)
            except Exception as e:
                logging.error(f"All attempts to save {period} chart failed: {e}")
                return None
            
        buf.seek(0)
        return buf
    except Exception as e:
        logging.error(f"Error generating {period} chart for guild {guild.id}: {e}", exc_info=True)
        return None
    finally:
        plt.close()

async def generate_relationship_network_graph(guild: discord.Guild, co_occurrence_data: dict, weekly_stats: dict) -> io.BytesIO | None:
    """Generates a network graph visualizing co-occurrence relationships 
       for top 10 by co-occurrence and top 10 distinct weekly active users.

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

    # Get top 10 weekly active users, excluding those already in the top co-occurrence list
    if weekly_stats is None: weekly_stats = {}
    # Sort all weekly users first
    sorted_weekly_users = sorted(weekly_stats.items(), key=lambda item: item[1], reverse=True)
    # Filter out those already selected and take top 10 of the remainder
    distinct_top_weekly_users = {uid for uid, _ in 
                                 [item for item in sorted_weekly_users if item[0] not in top_co_occurrence_users][:10]}
    logging.debug(f"Top 10 Distinct Weekly Active Users (IDs): {distinct_top_weekly_users}")

    # Combine the sets 
    selected_user_ids = top_co_occurrence_users.union(distinct_top_weekly_users)

    if len(selected_user_ids) < 2:
        logging.info(f"Not enough users selected ({len(selected_user_ids)}) based on criteria for guild {guild.id}. No graph generated.")
        return None
    logging.info(f"Selected {len(selected_user_ids)} users for relationship graph in guild {guild.id}. IDs: {selected_user_ids}")

    # --- Build Subgraph --- 
    try:
        await guild.chunk()
        members_map = {m.id: m for m in guild.members}
    except Exception as e:
        logging.warning(f"Error while chunking guild {guild.id}: {e}")
        members_map = {m.id: m for m in guild.members}

    G = nx.Graph()
    edges_data = []
    min_duration = float('inf')
    max_duration = 0.0
    nodes_added = set()
    
    for user_id in selected_user_ids:
        member = members_map.get(user_id)
        if member:
            name = get_preferred_name(member)
            G.add_node(user_id, label=name)
            nodes_added.add(user_id)
        else:
             logging.warning(f"Could not find member info for selected user ID {user_id}. Skipping.")
    
    if G.number_of_nodes() < 2:
        logging.info(f"Not enough valid nodes ({G.number_of_nodes()}) after fetch. No graph.")
        return None

    for m1_id in nodes_added:
        for m2_id in nodes_added:
            if m1_id >= m2_id: continue
            pair = tuple(sorted((m1_id, m2_id)))
            if pair in valid_pairs:
                 duration_seconds = co_occurrence_data.get(pair, 0.0)
                 if duration_seconds > 0:
                     G.add_edge(m1_id, m2_id, weight=duration_seconds)
                     edges_data.append(duration_seconds)
                     min_duration = min(min_duration, duration_seconds)
                     max_duration = max(max_duration, duration_seconds)
    
    if G.number_of_edges() == 0:
         logging.info(f"Selected users for guild {guild.id} have no co-occurrence edges. No graph.")
         return None

    # --- Graph Drawing --- 
    node_count = G.number_of_nodes()
    fig_size = min(30, max(20, node_count * 1.5))  # Dynamic figure size
    
    try:
        plt.figure(figsize=(fig_size, fig_size))

        # Layout calculation (remains the same)
        initial_pos = nx.circular_layout(G, scale=2.0)
        k_value = 15.0 / np.sqrt(node_count) if node_count > 0 else 2.0
        pos = nx.spring_layout(G, k=k_value, iterations=800, seed=42, pos=initial_pos, weight=None)
        scaling_factor = 1.3
        for node in pos:
            pos[node] = tuple(coord * scaling_factor for coord in pos[node])
        
        # Colors and node attributes (remains the same)
        colors = plt.get_cmap('tab20').colors 
        node_color_map = {node: colors[i % len(colors)] for i, node in enumerate(G.nodes())}
        node_colors = [node_color_map[node] for node in G.nodes()]
        node_shape = 'o'
        fixed_node_size = max(500, 1000 - (node_count * 25))
        
        # Edge attributes (remains the same)
        if max_duration <= min_duration:
             norm_weights = [0.5] * G.number_of_edges()
        else:
             power = 1.5 
             norm_weights = [((d['weight'] - min_duration) / (max_duration - min_duration)) ** power 
                           for u, v, d in G.edges(data=True)]
        edge_widths = [0.5 + w * 4.0 for w in norm_weights] 
        edge_alphas = [0.05 + w * 0.5 for w in norm_weights]

        # Suppress warnings during drawing operations
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", category=UserWarning)
            
            # Draw nodes
            nx.draw_networkx_nodes(G, pos, 
                                   node_size=fixed_node_size, 
                                   node_color=node_colors, 
                                   node_shape=node_shape, 
                                   alpha=0.9)

            # Draw edges
            nx.draw_networkx_edges(G, pos, width=edge_widths, alpha=edge_alphas, edge_color='darkgrey')

            # Draw labels with improved spacing and font size
            labels = nx.get_node_attributes(G, 'label')
            label_pos = {node: (coords[0], coords[1] + 0.07) for node, coords in pos.items()}
            font_size = max(7, 11 - (node_count * 0.2))
            
            # Safely add labels with fallback for problematic characters
            try:
                nx.draw_networkx_labels(G, label_pos, labels=labels, font_size=font_size, 
                                       font_family=font_prop.get_name() if font_prop else 'sans-serif',
                                       bbox=dict(facecolor='white', alpha=0.9, edgecolor='lightgrey', 
                                                boxstyle='round,pad=0.4'),
                                       verticalalignment='bottom')
            except Exception as label_error:
                logging.warning(f"Error drawing network labels with formatting: {label_error}")
                try:
                    nx.draw_networkx_labels(G, label_pos, labels=labels, font_size=font_size)
                except Exception as e:
                    logging.error(f"Even simplified label drawing failed: {e}")

        plt.title(f'{guild.name} - 成员关系网络图 (Top 10 Co + Top 10 Wkly)', fontsize=16, fontproperties=font_prop if font_prop else None)
        plt.axis('off')
        plt.xlim(-1.5, 1.5)
        plt.ylim(-1.5, 1.5)

        buf = io.BytesIO()
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                plt.savefig(buf, format='png', dpi=180, bbox_inches='tight')
        except Exception as save_error:
            logging.warning(f"Error saving relationship graph with bbox_inches='tight', trying without: {save_error}")
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    plt.savefig(buf, format='png', dpi=180)
            except Exception as e:
                logging.error(f"All attempts to save relationship graph failed: {e}")
                return None
                
        buf.seek(0)
        return buf

    except Exception as e:
        logging.error(f"Error generating relationship network graph for guild {guild.id}: {e}", exc_info=True)
        return None
    finally:
        plt.close() 