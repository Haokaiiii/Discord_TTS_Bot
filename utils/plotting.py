import logging
import os
import io
import gc
from typing import Dict, Tuple, Optional, List, Union
from contextlib import contextmanager
import matplotlib
# Use non-interactive backend to prevent GUI issues
matplotlib.use('Agg')

# Configure matplotlib for better performance and quality
matplotlib.rcParams.update({
    'figure.dpi': 100,
    'savefig.dpi': 150,
    'figure.max_open_warning': 20,
    'agg.path.chunksize': 10000,
    'font.size': 10,
    'axes.labelsize': 12,
    'axes.titlesize': 14,
    'xtick.labelsize': 10,
    'ytick.labelsize': 10,
    'legend.fontsize': 10,
    'figure.autolayout': False,  # We'll handle layout manually
    'axes.unicode_minus': False,
})

import matplotlib.pyplot as plt
import matplotlib.font_manager as fm
import seaborn as sns
import numpy as np
import pandas as pd
import discord
import networkx as nx
from collections import Counter
import warnings

from utils.config import FONT_PATH, MAX_PLOT_SIZE
from utils.helpers import get_preferred_name

# Global font property
font_prop = None

def setup_fonts():
    """Setup fonts with proper fallback handling."""
    global font_prop
    
    # Default font list
    font_list = ['DejaVu Sans', 'Arial Unicode MS', 'sans-serif']
    
    # Try to load Chinese font
    if os.path.exists(FONT_PATH):
        try:
            font_prop = fm.FontProperties(fname=FONT_PATH)
            font_name = font_prop.get_name()
            font_list.insert(0, font_name)
            logging.info(f"Loaded Chinese font: {font_name}")
        except Exception as e:
            logging.warning(f"Failed to load font from {FONT_PATH}: {e}")
            font_prop = None
    
    # Configure matplotlib
    plt.rcParams['font.sans-serif'] = font_list
    
    # Set seaborn style
    sns.set_theme(style="whitegrid", palette="deep")
    
    # Suppress font warnings
    warnings.filterwarnings("ignore", category=UserWarning, module="matplotlib")

# Initialize fonts
setup_fonts()

@contextmanager
def plot_context(title: str):
    """Context manager for plot creation with automatic cleanup."""
    fig = None
    try:
        logging.debug(f"Creating plot: {title}")
        fig = plt.figure()
        yield fig
    except Exception as e:
        logging.error(f"Error in plot context for '{title}': {e}", exc_info=True)
        raise
    finally:
        if fig:
            plt.close(fig)
            del fig
        gc.collect()

def save_plot_to_buffer(fig: plt.Figure, dpi: int = 150) -> Optional[io.BytesIO]:
    """Save a matplotlib figure to a BytesIO buffer with error handling."""
    buf = io.BytesIO()
    
    try:
        # Try with bbox_inches='tight' first
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            fig.savefig(buf, format='png', dpi=dpi, bbox_inches='tight', 
                       facecolor='white', edgecolor='none')
        buf.seek(0)
        return buf
        
    except Exception as e:
        logging.warning(f"Failed to save with bbox_inches='tight': {e}")
        
        # Try without bbox_inches
        buf = io.BytesIO()
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                fig.savefig(buf, format='png', dpi=dpi, 
                           facecolor='white', edgecolor='none')
            buf.seek(0)
            return buf
        except Exception as e2:
            logging.error(f"Failed to save plot: {e2}")
            return None

def validate_and_clean_data(data: pd.DataFrame, name: str) -> Optional[pd.DataFrame]:
    """Validate and clean a pandas DataFrame."""
    if not isinstance(data, pd.DataFrame):
        logging.error(f"Invalid data type for {name}: {type(data)}")
        return None
    
    if data.empty:
        logging.warning(f"Empty DataFrame for {name}")
        return None
    
    # Clean data
    if data.isnull().values.any():
        logging.debug(f"Cleaning NaN values in {name}")
        data = data.fillna(0)
    
    if np.isinf(data.values).any():
        logging.debug(f"Cleaning inf values in {name}")
        data = data.replace([np.inf, -np.inf], 0)
    
    return data

def create_heatmap(
    data: pd.DataFrame, 
    title: str, 
    color_map: str = "viridis", 
    annot: bool = True, 
    fmt: str = ".1f",
    vmin: Optional[float] = None,
    vmax: Optional[float] = None
) -> Optional[io.BytesIO]:
    """Create a heatmap with improved layout and error handling."""
    # Validate data
    data = validate_and_clean_data(data, title)
    if data is None:
        return None
    
    # Limit size if needed
    rows, cols = data.shape
    if rows > MAX_PLOT_SIZE or cols > MAX_PLOT_SIZE:
        logging.warning(f"Data too large ({rows}x{cols}), limiting to {MAX_PLOT_SIZE}")
        data = data.iloc[:MAX_PLOT_SIZE, :MAX_PLOT_SIZE]
        rows, cols = data.shape
    
    # Calculate figure size
    base_size = 0.5
    figsize = (
        max(8, min(20, cols * base_size + 2)),
        max(6, min(20, rows * base_size + 2))
    )
    
    with plot_context(title) as fig:
        ax = fig.add_subplot(111)
        
        # Create heatmap
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            
            # Determine if we should annotate
            should_annotate = annot and (rows * cols <= 625)  # Max 25x25
            
            sns.heatmap(
                data, 
                annot=should_annotate, 
                fmt=fmt, 
                cmap=color_map,
                cbar_kws={'label': '值'},
                square=False,
                linewidths=0.5 if rows <= 30 else 0,
                ax=ax,
                vmin=vmin,
                vmax=vmax
            )
        
        # Set title and labels
        ax.set_title(title, fontsize=14, fontproperties=font_prop, pad=20)
        
        # Rotate labels for readability
        plt.setp(ax.get_xticklabels(), rotation=45, ha='right', fontsize=8)
        plt.setp(ax.get_yticklabels(), rotation=0, fontsize=8)
        
        # Adjust layout
        try:
            fig.tight_layout(pad=2.0)
        except:
            fig.subplots_adjust(left=0.2, right=0.9, top=0.9, bottom=0.2)
        
        return save_plot_to_buffer(fig)

async def generate_co_occurrence_heatmap(
    guild: discord.Guild,
    co_occurrence_data: Dict[Tuple[int, int], float],
    member_period_voice_stats: Dict[int, float],
    relative: bool = False
) -> Optional[io.BytesIO]:
    """Generate co-occurrence heatmap with improved data processing."""
    logging.info(f"Generating {'relative' if relative else 'absolute'} heatmap for guild {guild.id}")
    
    # Validate input
    if not co_occurrence_data:
        logging.warning(f"No co-occurrence data for guild {guild.id}")
        return None
    
    if relative and not member_period_voice_stats:
        logging.warning(f"No voice stats for relative heatmap in guild {guild.id}")
        return None
    
    # Process co-occurrence data
    valid_pairs = {}
    member_ids = set()
    
    for (m1_id, m2_id), duration in co_occurrence_data.items():
        if duration > 0:
            key = tuple(sorted((m1_id, m2_id)))
            valid_pairs[key] = duration
            member_ids.update([m1_id, m2_id])
    
    if not valid_pairs:
        logging.info(f"No valid co-occurrence pairs for guild {guild.id}")
        return None
    
    # Get member information
    try:
        await guild.chunk()
    except:
        pass
    
    members_map = {m.id: m for m in guild.members}
    
    # Filter to members we have data for and can find
    active_members = sorted([mid for mid in member_ids if mid in members_map])
    
    if len(active_members) < 2:
        logging.info(f"Not enough members with data in guild {guild.id}")
        return None
    
    # Build matrix
    n = len(active_members)
    matrix = np.zeros((n, n))
    
    for i, m1_id in enumerate(active_members):
        for j, m2_id in enumerate(active_members):
            if i != j:
                pair = tuple(sorted((m1_id, m2_id)))
                duration_seconds = valid_pairs.get(pair, 0)
                
                if relative and m1_id in member_period_voice_stats:
                    total_time = member_period_voice_stats[m1_id]
                    if total_time > 0:
                        matrix[i, j] = (duration_seconds / total_time) * 100
                else:
                    matrix[i, j] = duration_seconds / 3600  # Convert to hours
    
    # Create DataFrame
    member_names = [get_preferred_name(members_map[mid]) for mid in active_members]
    df = pd.DataFrame(matrix, index=member_names, columns=member_names)
    
    # Limit size if needed
    if len(df) > MAX_PLOT_SIZE:
        # Get most active members
        row_sums = df.sum(axis=1)
        top_members = row_sums.nlargest(MAX_PLOT_SIZE).index
        df = df.loc[top_members, top_members]
        logging.info(f"Limited heatmap to top {MAX_PLOT_SIZE} members")
    
    # Generate heatmap
    if relative:
        title = f'{guild.name} - 成员共同在线时间比例'
        color_map = 'YlOrRd'
        fmt = '.1f%%'
        vmin, vmax = 0, 100
    else:
        title = f'{guild.name} - 成员共同在线时长'
        color_map = 'viridis'
        fmt = '.1f小时'
        vmin, vmax = None, None
    
    return create_heatmap(df, title, color_map, fmt=fmt, vmin=vmin, vmax=vmax)

async def generate_periodic_chart(
    guild: discord.Guild, 
    voice_stats_data: Dict[int, Dict[str, float]], 
    period: str
) -> Optional[io.BytesIO]:
    """Generate a bar chart for voice activity with improved styling."""
    period_names = {
        'daily': '今日', 'weekly': '本周', 'monthly': '本月',
        'yearly': '今年', 'total': '总计'
    }
    period_name = period_names.get(period, period)
    
    if not voice_stats_data:
        logging.info(f"No voice stats for guild {guild.id}")
        return None
    
    # Collect data
    data = []
    
    try:
        await guild.chunk()
    except:
        pass
    
    members_map = {m.id: m for m in guild.members}
    
    for member_id, stats in voice_stats_data.items():
        if isinstance(stats, dict):
            duration_seconds = stats.get(period, 0)
        else:
            duration_seconds = 0
            
        if duration_seconds > 60:  # At least 1 minute
            member = members_map.get(member_id)
            name = get_preferred_name(member) if member else f"用户 {member_id}"
            data.append({
                'member': name,
                'hours': duration_seconds / 3600
            })
    
    if not data:
        logging.info(f"No activity data for period {period} in guild {guild.id}")
        return None
    
    # Create DataFrame and sort
    df = pd.DataFrame(data)
    df = df.sort_values('hours', ascending=True).tail(30)  # Top 30
    
    # Calculate figure size
    n_members = len(df)
    figsize = (10, max(6, n_members * 0.3))
    
    with plot_context(f"{guild.name} {period_name} 语音活动") as fig:
        ax = fig.add_subplot(111)
        
        # Create horizontal bar chart
        bars = ax.barh(df['member'], df['hours'], color='steelblue', alpha=0.8)
        
        # Add value labels
        for bar in bars:
            width = bar.get_width()
            ax.text(width + 0.1, bar.get_y() + bar.get_height()/2,
                   f'{width:.1f}h', ha='left', va='center', fontsize=9)
        
        # Styling
        ax.set_xlabel('时长 (小时)', fontsize=12, fontproperties=font_prop)
        ax.set_ylabel('成员', fontsize=12, fontproperties=font_prop)
        ax.set_title(f'{guild.name} - {period_name}语音活动 Top {n_members}',
                    fontsize=14, fontproperties=font_prop, pad=20)
        
        # Grid
        ax.grid(True, axis='x', alpha=0.3)
        ax.set_axisbelow(True)
        
        # Adjust layout
        fig.tight_layout(pad=2.0)
        
        return save_plot_to_buffer(fig)

async def generate_relationship_network_graph(
    guild: discord.Guild,
    co_occurrence_data: Dict[Tuple[int, int], float],
    weekly_stats: Dict[int, float]
) -> Optional[io.BytesIO]:
    """Generate network graph with improved layout algorithm."""
    if not co_occurrence_data:
        logging.info(f"No co-occurrence data for guild {guild.id}")
        return None
    
    # Calculate total co-occurrence per user
    user_totals = Counter()
    valid_pairs = {}
    
    for (m1_id, m2_id), duration in co_occurrence_data.items():
        if duration >= 60:  # At least 1 minute
            user_totals[m1_id] += duration
            user_totals[m2_id] += duration
            valid_pairs[tuple(sorted((m1_id, m2_id)))] = duration
    
    # Select top users
    top_co_occurrence = {uid for uid, _ in user_totals.most_common(10)}
    
    # Add top weekly active users
    if weekly_stats:
        weekly_sorted = sorted(weekly_stats.items(), key=lambda x: x[1], reverse=True)
        top_weekly = {uid for uid, dur in weekly_sorted[:20] 
                     if uid not in top_co_occurrence and dur > 3600}[:10]
    else:
        top_weekly = set()
    
    selected_users = top_co_occurrence | top_weekly
    
    if len(selected_users) < 2:
        logging.info(f"Not enough users for network graph in guild {guild.id}")
        return None
    
    # Get member info
    try:
        await guild.chunk()
    except:
        pass
    
    members_map = {m.id: m for m in guild.members}
    
    # Build graph
    G = nx.Graph()
    
    # Add nodes
    valid_users = []
    for user_id in selected_users:
        if user_id in members_map:
            name = get_preferred_name(members_map[user_id])
            G.add_node(user_id, label=name)
            valid_users.append(user_id)
    
    if len(valid_users) < 2:
        return None
    
    # Add edges
    edge_weights = []
    for i, u1 in enumerate(valid_users):
        for u2 in valid_users[i+1:]:
            pair = tuple(sorted((u1, u2)))
            if pair in valid_pairs:
                weight = valid_pairs[pair]
                G.add_edge(u1, u2, weight=weight)
                edge_weights.append(weight)
    
    if not edge_weights:
        logging.info(f"No edges in network graph for guild {guild.id}")
        return None
    
    # Figure size
    n_nodes = len(G.nodes())
    figsize = min(16, max(10, n_nodes * 0.8))
    
    with plot_context(f"{guild.name} 关系网络") as fig:
        ax = fig.add_subplot(111)
        
        # Calculate layout using Kamada-Kawai for better results
        try:
            pos = nx.kamada_kawai_layout(G, weight='weight')
        except:
            # Fallback to spring layout
            pos = nx.spring_layout(G, k=2/np.sqrt(n_nodes), iterations=50)
        
        # Scale positions
        scale = 2.0
        pos = {node: (x * scale, y * scale) for node, (x, y) in pos.items()}
        
        # Node colors
        node_colors = plt.cm.Set3(np.linspace(0, 1, n_nodes))
        
        # Edge widths and alphas
        if len(edge_weights) > 1:
            min_w, max_w = min(edge_weights), max(edge_weights)
            if max_w > min_w:
                norm_weights = [(w - min_w) / (max_w - min_w) for w in edge_weights]
            else:
                norm_weights = [0.5] * len(edge_weights)
        else:
            norm_weights = [0.5] * len(edge_weights)
        
        edge_widths = [0.5 + w * 3 for w in norm_weights]
        edge_alphas = [0.2 + w * 0.6 for w in norm_weights]
        
        # Draw edges
        edges = G.edges()
        for (u, v), width, alpha in zip(edges, edge_widths, edge_alphas):
            ax.plot([pos[u][0], pos[v][0]], [pos[u][1], pos[v][1]], 
                   'gray', linewidth=width, alpha=alpha, zorder=1)
        
        # Draw nodes
        node_x = [pos[node][0] for node in G.nodes()]
        node_y = [pos[node][1] for node in G.nodes()]
        
        ax.scatter(node_x, node_y, c=node_colors, s=800, alpha=0.9, 
                  edgecolors='white', linewidth=2, zorder=2)
        
        # Draw labels
        labels = nx.get_node_attributes(G, 'label')
        for node, (x, y) in pos.items():
            ax.annotate(labels[node], (x, y), 
                       fontsize=10, ha='center', va='center',
                       fontproperties=font_prop, zorder=3)
        
        # Styling
        ax.set_title(f'{guild.name} - 成员关系网络图', 
                    fontsize=16, fontproperties=font_prop, pad=20)
        ax.axis('off')
        
        # Set axis limits with padding
        if node_x and node_y:
            x_margin = (max(node_x) - min(node_x)) * 0.2
            y_margin = (max(node_y) - min(node_y)) * 0.2
            ax.set_xlim(min(node_x) - x_margin, max(node_x) + x_margin)
            ax.set_ylim(min(node_y) - y_margin, max(node_y) + y_margin)
        
        fig.tight_layout(pad=1.0)
        
        return save_plot_to_buffer(fig, dpi=200)  # Higher DPI for network graph 