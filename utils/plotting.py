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
    
    # 重建字体缓存 - 使用兼容的方法
    try:
        # 尝试新版本的方法
        if hasattr(fm, 'fontManager'):
            fm.fontManager.__init__()
        else:
            # 回退到旧版本方法
            fm._rebuild()
    except Exception as e:
        logging.debug(f"Font cache rebuild failed: {e}")
    
    # Chinese font candidates with better paths
    chinese_fonts = [
        '/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc',
        '/usr/share/fonts/truetype/wqy/wqy-microhei.ttc', 
        '/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc',
        '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc',
        '/usr/share/fonts/truetype/noto-cjk/NotoSansCJK-Regular.ttc',
        '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'
    ]
    
    # System font names
    system_fonts = [
        'WenQuanYi Zen Hei', 'WenQuanYi Micro Hei', 'Noto Sans CJK SC',
        'SimHei', 'Microsoft YaHei', 'PingFang SC', 'Hiragino Sans GB'
    ]
    
    font_loaded = False
    
    # Try file-based fonts first
    for font_path in chinese_fonts:
        if os.path.exists(font_path):
            try:
                font_prop = fm.FontProperties(fname=font_path)
                font_name = font_prop.get_name()
                
                # Configure matplotlib with proper fallback chain
                plt.rcParams['font.sans-serif'] = [font_name, 'DejaVu Sans', 'Arial Unicode MS', 'Liberation Sans']
                plt.rcParams['font.family'] = 'sans-serif'
                
                # Test with Chinese characters
                test_fig, test_ax = plt.subplots(figsize=(1, 1))
                test_ax.text(0.5, 0.5, '测试中文', fontproperties=font_prop)
                plt.close(test_fig)
                
                logging.info(f"Successfully loaded Chinese font: {font_name} from {font_path}")
                font_loaded = True
                break
            except Exception as e:
                logging.debug(f"Failed to load font {font_path}: {e}")
                continue
    
    # Try system fonts if file-based fonts failed
    if not font_loaded:
        for font_name in system_fonts:
            try:
                available_fonts = [f.name for f in fm.fontManager.ttflist]
                if any(font_name.lower() in f.lower() for f in available_fonts):
                    plt.rcParams['font.sans-serif'] = [font_name, 'DejaVu Sans', 'Arial Unicode MS', 'Liberation Sans']
                    font_prop = fm.FontProperties(family=font_name)
                    logging.info(f"Added system font: {font_name}")
                    font_loaded = True
                    break
            except Exception as e:
                logging.debug(f"Font {font_name} not available: {e}")
                continue
    
    # Enhanced fallback configuration
    if not font_loaded:
        logging.warning("No Chinese fonts found, using fallback configuration")
        plt.rcParams['font.sans-serif'] = ['DejaVu Sans', 'Arial Unicode MS', 'Liberation Sans', 'sans-serif']
        font_prop = fm.FontProperties(family='DejaVu Sans')
    
    # Essential matplotlib configuration for Chinese support
    plt.rcParams.update({
        'axes.unicode_minus': False,
        'font.size': 11,
        'axes.labelsize': 12,
        'axes.titlesize': 14,
        'xtick.labelsize': 10,
        'ytick.labelsize': 10,
        'legend.fontsize': 10,
        'figure.titlesize': 16,
        'font.serif': ['DejaVu Serif', 'Times New Roman', 'serif'],
        'font.monospace': ['DejaVu Sans Mono', 'Courier New', 'monospace']
    })
    
    # 移除有问题的字体缓存刷新调用
    # matplotlib.font_manager._rebuild()  # 这行导致错误，已移除
    
    # Set seaborn style with better Chinese support
    sns.set_theme(style="whitegrid", palette="husl", font_scale=1.1)
    
    # Suppress font warnings more effectively
    warnings.filterwarnings("ignore", category=UserWarning, module="matplotlib")
    warnings.filterwarnings("ignore", message=".*Glyph.*missing from font.*")
    warnings.filterwarnings("ignore", message=".*findfont.*")
    
    logging.info("Font setup completed")

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
    color_map: str = "RdYlBu_r", 
    annot: bool = True, 
    fmt: str = ".1f",
    vmin: Optional[float] = None,
    vmax: Optional[float] = None
) -> Optional[io.BytesIO]:
    """Create a modern, beautiful heatmap with improved styling."""
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
    
    # Calculate dynamic figure size
    base_size = 0.6
    figsize = (
        max(10, min(24, cols * base_size + 4)),
        max(8, min(20, rows * base_size + 3))
    )
    
    with plot_context(title) as fig:
        fig.set_size_inches(figsize)
        fig.patch.set_facecolor('white')
        
        # Create subplot with better spacing
        ax = fig.add_subplot(111)
        
        # Modern color palettes
        modern_cmaps = {
            "RdYlBu_r": "RdYlBu_r",
            "viridis": "viridis", 
            "plasma": "plasma",
            "inferno": "inferno",
            "magma": "magma",
            "cividis": "cividis"
        }
        
        selected_cmap = modern_cmaps.get(color_map, color_map)
        
        # Create heatmap with enhanced styling
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            
            should_annotate = annot and (rows * cols <= 400)  # Max 20x20 for annotations
            
            # Create the heatmap
            im = sns.heatmap(
                data, 
                annot=should_annotate, 
                fmt=fmt, 
                cmap=selected_cmap,
                cbar_kws={
                    'label': '数值',
                    'shrink': 0.8,
                    'aspect': 20,
                    'pad': 0.02
                },
                square=False,
                linewidths=0.5 if rows <= 25 else 0.1,
                linecolor='white',
                ax=ax,
                vmin=vmin,
                vmax=vmax,
                annot_kws={'size': 8, 'weight': 'bold'} if should_annotate else None
            )
        
        # Enhanced title styling
        ax.set_title(
            title, 
            fontsize=16, 
            fontproperties=font_prop, 
            pad=25,
            weight='bold',
            color='#2E3440'
        )
        
        # Fix colorbar label font
        cbar = im.collections[0].colorbar
        if cbar:
            cbar.set_label('数值', fontproperties=font_prop)
        
        # Better label formatting with Chinese font support
        plt.setp(ax.get_xticklabels(), 
        rotation=45, 
        ha='right', 
        fontsize=9,
        weight='medium',
        fontproperties=font_prop)  # Add this line
        plt.setp(ax.get_yticklabels(), 
        rotation=0, 
        fontsize=9,
        weight='medium',
        fontproperties=font_prop)  # Add this line
        
        # Enhanced grid and spines
        ax.grid(False)
        for spine in ax.spines.values():
            spine.set_visible(False)
        
        # Improved layout
        try:
            fig.tight_layout(pad=3.0)
        except:
            fig.subplots_adjust(left=0.15, right=0.92, top=0.88, bottom=0.15)
        
        return save_plot_to_buffer(fig, dpi=200)

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
                    # For absolute heatmaps, make matrix symmetric
                    matrix[i, j] = duration_seconds / 3600  # Convert to hours
    
    # Create DataFrame with truncated names to handle long usernames
    member_names = []
    for mid in active_members:
        name = get_preferred_name(members_map[mid])
        # Truncate very long names to prevent layout issues
        if len(name) > 15:
            name = name[:12] + "..."
        member_names.append(name)
    
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
            if member:  # Only include current guild members for consistency
                name = get_preferred_name(member)
                # Truncate very long names to prevent layout issues
                if len(name) > 20:
                    name = name[:17] + "..."
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
        
        # Apply font properties to tick labels
        for label in ax.get_xticklabels():
            label.set_fontproperties(font_prop)
        for label in ax.get_yticklabels():
            label.set_fontproperties(font_prop)
        
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
    """Generate network graph with improved layout algorithm and user selection."""
    if not co_occurrence_data:
        logging.info(f"No co-occurrence data for guild {guild.id}")
        return None
    
    # Calculate total co-occurrence per user (but avoid double-counting pairs)
    user_pair_counts = Counter()  # Count unique pairs per user
    valid_pairs = {}
    
    for (m1_id, m2_id), duration in co_occurrence_data.items():
        if duration >= 60:  # At least 1 minute
            # Count unique pairs, not total duration to avoid bias toward high-duration pairs
            user_pair_counts[m1_id] += 1
            user_pair_counts[m2_id] += 1
            valid_pairs[tuple(sorted((m1_id, m2_id)))] = duration
    
    # Select top users by number of connections (more balanced than total duration)
    top_co_occurrence = {uid for uid, _ in user_pair_counts.most_common(10)}
    
    # Add top weekly active users - with improved logic
    if weekly_stats:
        weekly_sorted = sorted(weekly_stats.items(), key=lambda x: x[1], reverse=True)
        # Take top weekly users not already selected, with lower threshold for more users
        weekly_candidates = [
            uid for uid, dur in weekly_sorted[:30]  # Expanded from 20
            if uid not in top_co_occurrence and dur > 1800  # Reduced from 3600 (30 min instead of 1 hour)
        ]
        top_weekly = set(weekly_candidates[:10])
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
    
    # Add nodes with truncated names
    valid_users = []
    for user_id in selected_users:
        if user_id in members_map:
            name = get_preferred_name(members_map[user_id])
            # Truncate very long names for network graph
            if len(name) > 10:
                name = name[:8] + ".."
            G.add_node(user_id, label=name)
            valid_users.append(user_id)
    
    if len(valid_users) < 2:
        logging.info(f"After filtering, not enough valid users for network graph in guild {guild.id}")
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
        # Draw node labels with Chinese font support
        for node, (x, y) in pos.items():
        ax.text(x, y, labels[node], ha='center', va='center',
        fontsize=8, weight='bold', color='white',
        fontproperties=font_prop, zorder=3)
        
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

# Enhanced matplotlib configuration for modern plots
matplotlib.rcParams.update({
    'figure.dpi': 120,
    'savefig.dpi': 200,
    'figure.facecolor': 'white',
    'axes.facecolor': 'white',
    'axes.edgecolor': '#CCCCCC',
    'axes.linewidth': 0.8,
    'axes.grid': True,
    'axes.grid.axis': 'both',
    'grid.color': '#E5E5E5',
    'grid.linestyle': '-',
    'grid.linewidth': 0.5,
    'grid.alpha': 0.7,
    'xtick.color': '#666666',
    'ytick.color': '#666666',
    'text.color': '#2E3440',
    'axes.labelcolor': '#2E3440',
    'axes.titlecolor': '#2E3440'
})