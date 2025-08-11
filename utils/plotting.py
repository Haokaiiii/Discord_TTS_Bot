"""Plot generation utilities for the Discord TTS bot.

This module centralizes all visualization logic used across the project.
It focuses on memory-safe, backend-agnostic figure generation and includes
performance optimizations for large plots.

Notes
-----
- The non-interactive Matplotlib backend is enforced via ``Agg`` to work in
  headless environments (e.g., Docker, CI, bot runtime).
- Functions return in-memory PNG buffers (``io.BytesIO``) suitable for direct
  upload without writing to disk.
"""
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
from matplotlib.collections import LineCollection
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

# Public API of this module
__all__ = [
    'setup_fonts',
    'plot_context',
    'save_plot_to_buffer',
    'validate_and_clean_data',
    'calculate_optimal_font_size',
    'smart_text_truncation',
    'auto_adjust_figure_size',
    'adjust_text_properties',
    'create_heatmap',
    'generate_co_occurrence_heatmap',
    'generate_periodic_chart',
    'filter_edges_by_strength',
    'calculate_node_importance',
    'detect_communities',
    'create_community_colors',
    'apply_hierarchical_layout',
    'create_edge_bundling',
    'add_interactive_elements_info',
    'apply_node_separation',
    'generate_relationship_network_graph',
    'create_enhanced_community_colors',
]

def setup_fonts():
    """Initialize font configuration with Chinese-support fallbacks.

    Sets up Matplotlib and Seaborn to handle multilingual text gracefully.
    Attempts to load common Chinese fonts from file paths and system font
    names. Falls back to unicode-capable defaults if none are found.

    Returns
    -------
    None
        Fonts and Matplotlib/Seaborn global settings are configured in-place.
    """
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
    """Context manager for safe plot creation and cleanup.

    Parameters
    ----------
    title : str
        Logical title of the plot. Only used for logging.

    Yields
    ------
    matplotlib.figure.Figure
        The created figure instance for plotting.
    """
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

def save_plot_to_buffer(
    fig: plt.Figure,
    dpi: int = 150,
    use_tight_layout: bool = True,
) -> Optional[io.BytesIO]:
    """Serialize a figure to an in-memory PNG buffer.

    Parameters
    ----------
    fig : matplotlib.figure.Figure
        Figure to serialize.
    dpi : int, default 150
        Resolution for the saved image.
    use_tight_layout : bool, default True
        If True, save with ``bbox_inches='tight'`` to minimize excess margins.
        Can be disabled for large/complex figures to improve speed.

    Returns
    -------
    io.BytesIO or None
        PNG image buffer on success; None if saving failed.
    """
    buf = io.BytesIO()

    def _save(with_tight: bool) -> bool:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter('ignore')
                fig.savefig(
                    buf,
                    format='png',
                    dpi=dpi,
                    bbox_inches='tight' if with_tight else None,
                    facecolor='white',
                    edgecolor='none',
                )
            return True
        except Exception as exc:  # noqa: BLE001 - log and fallback
            logging.warning(
                "Failed to save figure with%s tight bbox: %s",
                '' if with_tight else 'out',
                exc,
            )
            return False

    ok = _save(use_tight_layout)
    if not ok:
        buf = io.BytesIO()  # reset buffer before retry
        ok = _save(False)

    if ok:
        buf.seek(0)
        return buf
    return None

def validate_and_clean_data(data: pd.DataFrame, name: str) -> Optional[pd.DataFrame]:
    """Validate and sanitize a DataFrame used for plotting.

    Parameters
    ----------
    data : pandas.DataFrame
        Input data expected for the visualization.
    name : str
        Logical dataset name for logging context.

    Returns
    -------
    pandas.DataFrame or None
        Cleaned DataFrame if valid and non-empty, otherwise None.
    """
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

# Add these helper functions after the existing helper functions (around line 200)

def calculate_optimal_font_size(text_length: int, available_space: float, base_size: int = 10) -> int:
    """Heuristically choose a readable font size.

    Parameters
    ----------
    text_length : int
        Number of characters that must fit.
    available_space : float
        Approximate available width/height for the text.
    base_size : int, default 10
        Baseline font size used for short text.

    Returns
    -------
    int
        Suggested font size in points.
    """
    if text_length <= 8:
        return base_size
    elif text_length <= 15:
        return max(6, base_size - 2)
    elif text_length <= 25:
        return max(5, base_size - 4)
    else:
        return max(4, base_size - 6)

def smart_text_truncation(text: str, max_length: int = 15, preserve_words: bool = True) -> str:
    """Truncate text while preserving readability when possible.

    Parameters
    ----------
    text : str
        Input title/label text.
    max_length : int, default 15
        Maximum resulting character length.
    preserve_words : bool, default True
        If True, attempts to retain whole words.

    Returns
    -------
    str
        Possibly shortened text with ellipsis if needed.
    """
    if len(text) <= max_length:
        return text
    
    if preserve_words and ' ' in text:
        words = text.split()
        if len(words) > 1:
            # Try to keep first and last word
            first_word = words[0]
            last_word = words[-1]
            if len(first_word) + len(last_word) + 3 <= max_length:
                return f"{first_word}...{last_word}"
            # Otherwise just use first word
            elif len(first_word) + 3 <= max_length:
                return f"{first_word}..."
    
    # Fallback to simple truncation
    return text[:max_length-3] + "..." if len(text) > max_length else text

def auto_adjust_figure_size(n_items: int, item_type: str = 'bar') -> tuple:
    """Compute a reasonable figure size based on content density.

    Parameters
    ----------
    n_items : int
        Number of primary visual elements (e.g., bars, heatmap cells side).
    item_type : {'bar', 'heatmap', 'network'}, default 'bar'
        Visualization type that guides the sizing heuristic.

    Returns
    -------
    tuple
        Figure size as ``(width, height)`` in inches.
    """
    if item_type == 'bar':
        width = max(8, min(16, n_items * 0.4 + 6))
        height = max(6, min(20, n_items * 0.35 + 4))
    elif item_type == 'heatmap':
        size = max(8, min(20, n_items * 0.6 + 4))
        width = height = size
    elif item_type == 'network':
        size = max(10, min(16, n_items * 0.8))
        width = height = size
    else:
        width, height = 10, 8
    
    return (width, height)

def adjust_text_properties(ax, text_elements: list, available_space: tuple):
    """Reduce label collisions by adjusting font sizes and truncating.

    Parameters
    ----------
    ax : matplotlib.axes.Axes
        Target axes whose labels are being adjusted.
    text_elements : list
        List of text artists to consider.
    available_space : tuple
        Available figure space as ``(width, height)`` in inches.

    Returns
    -------
    None
    """
    width, height = available_space
    
    for text_elem in text_elements:
        if hasattr(text_elem, 'get_text'):
            text = text_elem.get_text()
            current_size = text_elem.get_fontsize()
            
            # Calculate optimal size based on available space
            optimal_size = calculate_optimal_font_size(len(text), width/len(text_elements))
            new_size = min(current_size, optimal_size)
            
            text_elem.set_fontsize(new_size)
            
            # Truncate if still too long
            if len(text) > 20:
                text_elem.set_text(smart_text_truncation(text, 18))

# Update the create_heatmap function
def create_heatmap(
    data: pd.DataFrame, 
    title: str, 
    color_map: str = "RdYlBu_r", 
    annot: bool = True, 
    fmt: str = ".1f",
    vmin: Optional[float] = None,
    vmax: Optional[float] = None
) -> Optional[io.BytesIO]:
    """Create a styled heatmap image buffer.

    Parameters
    ----------
    data : pandas.DataFrame
        Heatmap values; row/column labels are taken from the index/columns.
    title : str
        Title shown above the heatmap.
    color_map : str, default 'RdYlBu_r'
        Matplotlib colormap name.
    annot : bool, default True
        If True, draw text annotations for each cell (capped for large grids).
    fmt : str, default '.1f'
        Numeric format string for annotations.
    vmin, vmax : float, optional
        Color scale limits.

    Returns
    -------
    io.BytesIO or None
        PNG image buffer on success; None if inputs are invalid.
    """
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
    
    # Automatically adjust figure size
    figsize = auto_adjust_figure_size(max(rows, cols), 'heatmap')
    
    # Smart label truncation for better readability
    data.index = [smart_text_truncation(str(idx), 12) for idx in data.index]
    data.columns = [smart_text_truncation(str(col), 12) for col in data.columns]
    
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
        
        # Determine annotation settings based on size
        should_annotate = annot and (rows * cols <= 400)  # Max 20x20 for annotations
        annot_fontsize = calculate_optimal_font_size(max(rows, cols), min(figsize), 8)
        
        # Create the heatmap
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            
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
                annot_kws={'size': annot_fontsize, 'weight': 'bold'} if should_annotate else None
            )

        # Rasterize large heatmaps to speed up rendering and reduce size
        total_cells = rows * cols
        if total_cells >= 2000:
            try:
                # QuadMesh is under the first collection
                im.collections[0].set_rasterized(True)
                if im.collections[0].colorbar and im.collections[0].colorbar.ax:
                    im.collections[0].colorbar.ax.set_rasterized(True)
            except Exception:
                pass
        
        # Enhanced title styling with automatic font size
        title_fontsize = calculate_optimal_font_size(len(title), figsize[0], 16)
        ax.set_title(
            title, 
            fontsize=title_fontsize, 
            fontproperties=font_prop, 
            pad=25,
            weight='bold',
            color='#2E3440'
        )
        
        # Fix colorbar label font
        cbar = im.collections[0].colorbar
        if cbar:
            cbar.set_label('数值', fontproperties=font_prop)
        
        # Better label formatting with automatic font sizing
        label_fontsize = calculate_optimal_font_size(max(len(str(l)) for l in data.index), figsize[0]/cols, 9)
        
        plt.setp(ax.get_xticklabels(), 
                rotation=45, 
                ha='right', 
                fontsize=label_fontsize,
                weight='medium',
                fontproperties=font_prop)
        plt.setp(ax.get_yticklabels(), 
                rotation=0, 
                fontsize=label_fontsize,
                weight='medium',
                fontproperties=font_prop)
        
        # Auto-adjust text to prevent overlap
        adjust_text_properties(ax, ax.get_xticklabels() + ax.get_yticklabels(), figsize)
        
        # Enhanced grid and spines
        ax.grid(False)
        for spine in ax.spines.values():
            spine.set_visible(False)
        
        # Improved layout with automatic adjustment
        try:
            fig.tight_layout(pad=3.0)
        except Exception:
            fig.subplots_adjust(left=0.15, right=0.92, top=0.88, bottom=0.15)
        
        # For large heatmaps, skipping tight bbox can speed up saving
        return save_plot_to_buffer(fig, dpi=200, use_tight_layout=total_cells < 2000)

async def generate_co_occurrence_heatmap(
    guild: discord.Guild,
    co_occurrence_data: Dict[Tuple[int, int], float],
    member_period_voice_stats: Dict[int, float],
    relative: bool = False
) -> Optional[io.BytesIO]:
    """Generate a co-occurrence heatmap for guild members.

    Parameters
    ----------
    guild : discord.Guild
        Guild whose members are visualized.
    co_occurrence_data : dict
        Mapping ``(member_id_1, member_id_2) -> seconds together``.
    member_period_voice_stats : dict
        Mapping ``member_id -> seconds in period`` for relative percentage view.
    relative : bool, default False
        If True, normalize each row by the member's total period time (percent).

    Returns
    -------
    io.BytesIO or None
        PNG image buffer on success; None if insufficient data.
    """
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
    
    # Create DataFrame with smart name truncation
    member_names = []
    for mid in active_members:
        name = get_preferred_name(members_map[mid])
        # Smart truncation based on total number of members
        max_length = max(8, 20 - len(active_members) // 5)  # Shorter names for more members
        name = smart_text_truncation(name, max_length)
        member_names.append(name)
    
    df = pd.DataFrame(matrix, index=member_names, columns=member_names)
    
    # Limit size if needed
    if len(df) > MAX_PLOT_SIZE:
        # Get most active members
        row_sums = df.sum(axis=1)
        top_members = row_sums.nlargest(MAX_PLOT_SIZE).index
        df = df.loc[top_members, top_members]
        logging.info(f"Limited heatmap to top {MAX_PLOT_SIZE} members")
    
    # Generate heatmap with automatic adjustments
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
    """Generate a horizontal bar chart for voice activity by period.

    Parameters
    ----------
    guild : discord.Guild
        Guild whose members are visualized.
    voice_stats_data : dict
        Mapping ``member_id -> {period: seconds, ...}``.
    period : str
        One of ``'daily'``, ``'weekly'``, ``'monthly'``, ``'yearly'``, ``'total'``.

    Returns
    -------
    io.BytesIO or None
        PNG image buffer on success; None if insufficient data.
    """
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
                # Smart truncation based on number of members
                max_length = max(10, 25 - len(data) // 10)
                name = smart_text_truncation(name, max_length)
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
    
    # Auto-calculate figure size
    n_members = len(df)
    figsize = auto_adjust_figure_size(n_members, 'bar')
    
    with plot_context(f"{guild.name} {period_name} 语音活动") as fig:
        fig.set_size_inches(figsize)
        ax = fig.add_subplot(111)
        
        # Create horizontal bar chart
        bars = ax.barh(df['member'], df['hours'], color='steelblue', alpha=0.8)
        
        # Add value labels with automatic font sizing
        label_fontsize = calculate_optimal_font_size(n_members, figsize[0], 9)
        for bar in bars:
            width = bar.get_width()
            ax.text(width + max(0.1, width * 0.02), bar.get_y() + bar.get_height()/2,
                   f'{width:.1f}h', ha='left', va='center', fontsize=label_fontsize)
        
        # Styling with automatic font sizing
        title_fontsize = calculate_optimal_font_size(len(f'{guild.name} - {period_name}语音活动'), figsize[0], 14)
        axis_fontsize = calculate_optimal_font_size(n_members, figsize[1], 12)
        
        ax.set_xlabel('时长 (小时)', fontsize=axis_fontsize, fontproperties=font_prop)
        ax.set_ylabel('成员', fontsize=axis_fontsize, fontproperties=font_prop)
        ax.set_title(f'{guild.name} - {period_name}语音活动 Top {n_members}',
                    fontsize=title_fontsize, fontproperties=font_prop, pad=20)
        
        # Apply font properties to tick labels with automatic sizing
        tick_fontsize = calculate_optimal_font_size(max(len(name) for name in df['member']), figsize[1]/n_members, 10)
        for label in ax.get_xticklabels():
            label.set_fontproperties(font_prop)
            label.set_fontsize(tick_fontsize)
        for label in ax.get_yticklabels():
            label.set_fontproperties(font_prop)
            label.set_fontsize(tick_fontsize)
        
        # Auto-adjust text to prevent overlap
        adjust_text_properties(ax, ax.get_yticklabels(), figsize)
        
        # Grid
        ax.grid(True, axis='x', alpha=0.3)
        ax.set_axisbelow(True)
        
        # Adjust layout
        fig.tight_layout(pad=2.0)
        
        return save_plot_to_buffer(fig, use_tight_layout=True)

# Add these helper functions for network graph improvements
def filter_edges_by_strength(G, edge_weights, keep_percentage=0.3):
    """Keep only the strongest edges to reduce clutter.

    Parameters
    ----------
    G : networkx.Graph
        Graph containing edges.
    edge_weights : list[float]
        Edge weights aligned with the order of ``G.edges()`` at construction time.
    keep_percentage : float, default 0.3
        Fraction of strongest edges to retain (0-1).

    Returns
    -------
    tuple[networkx.Graph, list[float]]
        Filtered graph and corresponding retained weights.
    """
    if not edge_weights:
        return G, []
    
    # Sort edges by weight and keep top percentage
    sorted_edges = sorted(zip(G.edges(), edge_weights), key=lambda x: x[1], reverse=True)
    keep_count = max(1, int(len(sorted_edges) * keep_percentage))
    
    # Create new graph with only strong edges
    G_filtered = nx.Graph()
    G_filtered.add_nodes_from(G.nodes(data=True))
    
    filtered_weights = []
    for (u, v), weight in sorted_edges[:keep_count]:
        G_filtered.add_edge(u, v, weight=weight)
        filtered_weights.append(weight)
    
    return G_filtered, filtered_weights

def calculate_node_importance(G, weekly_stats=None):
    """Estimate node importance using centrality and activity.

    Parameters
    ----------
    G : networkx.Graph
        Relationship graph of members.
    weekly_stats : dict, optional
        Mapping ``member_id -> seconds this week``.

    Returns
    -------
    dict
        Mapping ``node -> importance score in [0, 1]``.
    """
    importance = {}
    
    # Calculate centrality measures
    try:
        betweenness = nx.betweenness_centrality(G)
        degree = dict(G.degree())
        
        for node in G.nodes():
            # Combine centrality with activity if available
            base_importance = (betweenness.get(node, 0) * 0.6 + 
                             (degree.get(node, 0) / max(degree.values()) if degree.values() else 0) * 0.4)
            
            # Boost with weekly activity
            if weekly_stats and node in weekly_stats:
                activity_boost = min(1.0, weekly_stats[node] / 3600)  # Normalize to hours
                importance[node] = base_importance * 0.7 + activity_boost * 0.3
            else:
                importance[node] = base_importance
                
    except:
        # Fallback to simple degree centrality
        degree = dict(G.degree())
        max_degree = max(degree.values()) if degree.values() else 1
        importance = {node: deg / max_degree for node, deg in degree.items()}
    
    return importance

def detect_communities(G):
    """Detect communities for better layout grouping.

    Parameters
    ----------
    G : networkx.Graph
        Input graph.

    Returns
    -------
    tuple[dict, int]
        Mapping ``node -> community_id`` and number of communities.
    """
    try:
        import networkx.algorithms.community as nx_comm
        communities = list(nx_comm.greedy_modularity_communities(G))
        
        # Create community mapping
        community_map = {}
        for i, community in enumerate(communities):
            for node in community:
                community_map[node] = i
        
        return community_map, len(communities)
    except:
        # Fallback: no community detection
        return {node: 0 for node in G.nodes()}, 1

def create_community_colors(num_communities):
    """Create distinct colors for communities.

    Parameters
    ----------
    num_communities : int
        Number of distinct groups.

    Returns
    -------
    list[str]
        Hex RGB color strings sized to ``num_communities``.
    """
    if num_communities <= 1:
        return ['#1f77b4']  # Single blue color
    
    # Use a colormap that provides good contrast
    import matplotlib.cm as cm
    colors = cm.Set3(np.linspace(0, 1, min(num_communities, 12)))
    return [f'#{int(r*255):02x}{int(g*255):02x}{int(b*255):02x}' for r, g, b, _ in colors]

# 在现有的 filter_edges_by_strength 函数后添加更多辅助函数

def apply_hierarchical_layout(G, community_map, pos_base):
    """应用分层布局，将同社区节点聚集在一起，但保持足够间距。

    Parameters
    ----------
    G : networkx.Graph
        输入图。
    community_map : dict
        ``node -> 社区编号`` 的映射。
    pos_base : dict
        初始位置 ``node -> (x, y)``。

    Returns
    -------
    dict
        改善后的节点坐标映射。
    """
    communities = {}
    for node, comm in community_map.items():
        if comm not in communities:
            communities[comm] = []
        communities[comm].append(node)
    
    # 为每个社区分配圆形区域
    num_communities = len(communities)
    if num_communities <= 1:
        return pos_base
    
    import math
    pos_improved = {}
    
    # 增加社区间的距离
    community_radius = 4  # 从3增加到4
    
    for i, (comm_id, nodes) in enumerate(communities.items()):
        # 计算社区中心位置（圆形排列）
        angle = 2 * math.pi * i / num_communities
        center_x = community_radius * math.cos(angle)
        center_y = community_radius * math.sin(angle)
        
        # 在社区内部使用子布局，增加k值
        if len(nodes) > 1:
            subgraph = G.subgraph(nodes)
            try:
                # 增加社区内部的间距
                sub_pos = nx.spring_layout(subgraph, k=1.5, iterations=100)
            except:
                sub_pos = {node: (0, 0) for node in nodes}
        else:
            sub_pos = {nodes[0]: (0, 0)}
        
        # 将子布局位置调整到社区中心，增加缩放因子
        for node in nodes:
            x, y = sub_pos.get(node, (0, 0))
            pos_improved[node] = (center_x + x * 1.2, center_y + y * 1.2)  # 从0.8增加到1.2
    
    return pos_improved

def create_edge_bundling(G, pos, edge_weights):
    """创建边的捆绑效果，减少视觉混乱。

    Parameters
    ----------
    G : networkx.Graph
        输入图。
    pos : dict
        节点坐标 ``node -> (x, y)``。
    edge_weights : list[float]
        边权重列表，与 ``G.edges()`` 顺序一致。

    Returns
    -------
    list[dict]
        边的几何信息列表，用于高级绘制效果。
    """
    bundled_edges = []
    
    for i, (u, v) in enumerate(G.edges()):
        x1, y1 = pos[u]
        x2, y2 = pos[v]
        weight = edge_weights[i] if i < len(edge_weights) else 0.5
        
        # 为强连接创建轻微弯曲，弱连接保持直线
        if weight > 0.7:  # 强连接
            # 计算控制点创建贝塞尔曲线
            mid_x, mid_y = (x1 + x2) / 2, (y1 + y2) / 2
            # 添加垂直偏移
            dx, dy = x2 - x1, y2 - y1
            length = (dx**2 + dy**2)**0.5
            if length > 0:
                offset_x = -dy / length * 0.3
                offset_y = dx / length * 0.3
                control_x = mid_x + offset_x
                control_y = mid_y + offset_y
                bundled_edges.append({
                    'type': 'curve',
                    'start': (x1, y1),
                    'control': (control_x, control_y),
                    'end': (x2, y2),
                    'weight': weight
                })
            else:
                bundled_edges.append({
                    'type': 'line',
                    'start': (x1, y1),
                    'end': (x2, y2),
                    'weight': weight
                })
        else:  # 弱连接保持直线
            bundled_edges.append({
                'type': 'line',
                'start': (x1, y1),
                'end': (x2, y2),
                'weight': weight
            })
    
    return bundled_edges

def add_interactive_elements_info(ax, G, node_importance, community_map):
    """添加交互式信息面板（静态版本的信息展示）。

    Parameters
    ----------
    ax : matplotlib.axes.Axes
        目标坐标轴。
    G : networkx.Graph
        图对象。
    node_importance : dict
        节点重要性映射。
    community_map : dict
        节点对应的社区编号。
    """
    # 创建信息文本框
    info_text = []
    info_text.append(f"Network Statistics:")
    info_text.append(f"• Nodes: {len(G.nodes())}")
    info_text.append(f"• Edges: {len(G.edges())}")
    info_text.append(f"• Communities: {len(set(community_map.values()))}")
    
    # 添加最重要的3个节点信息
    top_nodes = sorted(node_importance.items(), key=lambda x: x[1], reverse=True)[:3]
    info_text.append(f"\nTop Members:")
    for i, (node_id, importance) in enumerate(top_nodes, 1):
        node_label = G.nodes[node_id].get('label', str(node_id))
        info_text.append(f"{i}. {node_label}")
    
    # 在图的左上角添加信息框 - FIX: Add font properties for Chinese support
    info_str = "\n".join(info_text)
    ax.text(0.02, 0.98, info_str, transform=ax.transAxes, 
           verticalalignment='top', horizontalalignment='left',
           bbox=dict(boxstyle='round,pad=0.5', facecolor='white', 
                    alpha=0.9, edgecolor='#CCCCCC'),
           fontsize=8, fontfamily='monospace', fontproperties=font_prop)

def apply_node_separation(pos, min_distance=0.1):
    """Apply minimum distance constraint between nodes to prevent overlap.

    Parameters
    ----------
    pos : dict
        节点坐标 ``node -> (x, y)``。
    min_distance : float, default 0.1
        节点之间的最小目标距离。

    Returns
    -------
    dict
        调整后的节点坐标。
    """
    import itertools
    
    # Convert to list for easier manipulation
    nodes = list(pos.keys())
    positions = {node: list(pos[node]) for node in nodes}
    
    # Apply separation force iteratively
    for iteration in range(50):  # Maximum iterations to prevent infinite loop
        moved = False
        
        for node1, node2 in itertools.combinations(nodes, 2):
            x1, y1 = positions[node1]
            x2, y2 = positions[node2]
            
            # Calculate distance
            dx = x2 - x1
            dy = y2 - y1
            distance = (dx**2 + dy**2)**0.5
            
            # If too close, push apart
            if distance < min_distance and distance > 0:
                # Calculate push direction
                push_x = (dx / distance) * (min_distance - distance) * 0.5
                push_y = (dy / distance) * (min_distance - distance) * 0.5
                
                # Move nodes apart
                positions[node1][0] -= push_x
                positions[node1][1] -= push_y
                positions[node2][0] += push_x
                positions[node2][1] += push_y
                
                moved = True
        
        # If no nodes moved, we're done
        if not moved:
            break
    
    # Convert back to tuples
    return {node: tuple(positions[node]) for node in nodes}

async def generate_relationship_network_graph(
    guild: discord.Guild,
    co_occurrence_data: Dict[Tuple[int, int], float],
    weekly_stats: Dict[int, float]
) -> Optional[io.BytesIO]:
    """Generate a relationship network graph for selected guild members.

    Parameters
    ----------
    guild : discord.Guild
        Guild whose members are visualized.
    co_occurrence_data : dict
        ``(member_id_1, member_id_2) -> seconds together``.
    weekly_stats : dict
        ``member_id -> seconds active in the past week``.

    Returns
    -------
    io.BytesIO or None
        PNG image buffer on success; None if insufficient data.
    """
    if not co_occurrence_data:
        logging.info(f"No co-occurrence data for guild {guild.id} to generate network graph.")
        return None

    # --- Node Selection ---
    # Calculate total co-occurrence time per user
    total_co_occurrence_per_user = Counter()
    valid_pairs = set()
    for (m1_id, m2_id), duration_seconds in co_occurrence_data.items():
        if duration_seconds >= 60:  # Only consider pairs with >= 1 min co-occurrence
            total_co_occurrence_per_user[m1_id] += duration_seconds
            total_co_occurrence_per_user[m2_id] += duration_seconds
            valid_pairs.add(tuple(sorted((m1_id, m2_id))))

    # Get top 10 by total co-occurrence
    top_co_occurrence_users = {uid for uid, _ in total_co_occurrence_per_user.most_common(10)}
    logging.debug(f"Top 10 Co-occurrence Users (IDs): {top_co_occurrence_users}")

    # Get top 10 weekly active users, excluding those already in the top co-occurrence list
    if weekly_stats is None:
        weekly_stats = {}
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
            # Smart truncation for better readability
            name = smart_text_truncation(name, 12, preserve_words=True)
            G.add_node(user_id, label=name)
            nodes_added.add(user_id)
        else:
            logging.warning(f"Could not find member info for selected user ID {user_id}. Skipping.")

    if G.number_of_nodes() < 2:
        logging.info(f"Not enough valid nodes ({G.number_of_nodes()}) after fetch. No graph.")
        return None

    for m1_id in nodes_added:
        for m2_id in nodes_added:
            if m1_id >= m2_id:
                continue
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

    # Optionally prune weaker edges for clarity/performance
    G, edges_data = filter_edges_by_strength(G, edges_data, keep_percentage=0.5)

    # Calculate node importance and detect communities on the filtered graph
    node_importance = calculate_node_importance(G, weekly_stats)
    community_map, num_communities = detect_communities(G)
    
    # Create vibrant, distinct colors for communities
    community_colors = create_enhanced_community_colors(num_communities)

    # --- Graph Drawing ---
    node_count = G.number_of_nodes()
    fig_size = min(20, max(12, node_count * 1.2))  # Dynamic figure size

    with plot_context(f"{guild.name} 关系网络") as fig:
        fig.set_size_inches(fig_size, fig_size)
        ax = fig.add_subplot(111)
        ax.set_facecolor('#FAFAFA')  # Light background

        # Improved layout calculation to prevent overlapping
        logging.debug(f"[Network Graph] Calculating layout for {node_count} nodes")

        # Step 1: Start with a circular layout to ensure initial separation
        initial_pos = nx.circular_layout(G, scale=3.0)  # Increased scale from 2.0 to 3.0

        # Step 2: Apply spring layout with dynamic iterations and higher repulsion
        k_value = 25.0 / np.sqrt(node_count) if node_count > 0 else 5.0
        iterations = 600 if node_count > 50 else 1000
        pos = nx.spring_layout(
            G,
            k=k_value,
            iterations=iterations,
            seed=42,
            pos=initial_pos,
            weight='weight',
        )

        # Step 3: Enhance separation by applying scaling
        scaling_factor = 1.6  # Increased from 1.3 to 1.6
        pos = {node: (coords[0] * scaling_factor, coords[1] * scaling_factor) for node, coords in pos.items()}

        # Step 4: Apply node separation logic (using existing function)
        pos = apply_node_separation(pos, min_distance=0.2)

        # Draw edges efficiently using a LineCollection with per-edge styles
        edge_weights_normalized = [
            (w - min_duration) / (max_duration - min_duration) if max_duration > min_duration else 0.5
            for w in edges_data
        ]

        segments = []
        colors = []
        linewidths = []
        for i, (u, v) in enumerate(G.edges()):
            x1, y1 = pos[u]
            x2, y2 = pos[v]
            weight = edge_weights_normalized[i]
            segments.append([(x1, y1), (x2, y2)])
            alpha = 0.3 + weight * 0.5
            colors.append((0.4, 0.4, 0.4, alpha))
            linewidths.append(1.0 + weight * 4.0)

        if segments:
            lc = LineCollection(segments, colors=colors, linewidths=linewidths, zorder=1)
            ax.add_collection(lc)

        # Prepare enhanced node visual attributes
        node_colors = []
        node_sizes = []
        node_alphas = []
        
        for node in G.nodes():
            # Color based on community
            community_id = community_map.get(node, 0)
            node_colors.append(community_colors[community_id % len(community_colors)])
            
            # Size based on importance (400-1500 range)
            importance = node_importance.get(node, 0)
            node_sizes.append(400 + importance * 1100)
            
            # Alpha based on activity
            activity = weekly_stats.get(node, 0)
            max_activity = max(weekly_stats.values()) if weekly_stats else 1
            node_alphas.append(0.7 + (activity / max_activity) * 0.3)

        # Draw node shadows for depth
        node_x = [pos[node][0] for node in G.nodes()]
        node_y = [pos[node][1] for node in G.nodes()]
        shadow_offset = 0.05
        
        ax.scatter([x + shadow_offset for x in node_x], [y - shadow_offset for y in node_y],
                  c='#00000030', s=node_sizes, zorder=2)

        # Draw main nodes with enhanced styling
        scatter = ax.scatter(node_x, node_y, 
                           c=node_colors, 
                           s=node_sizes, 
                           alpha=node_alphas,
                           edgecolors='white', 
                           linewidth=3, 
                           zorder=3)

        # Add gradient effect to nodes
        for i, node in enumerate(G.nodes()):
            x, y = pos[node]
            size = node_sizes[i]
            color = node_colors[i]
            
            # Inner highlight circle
            ax.scatter(x, y, 
                      c='white', 
                      s=size * 0.3, 
                      alpha=0.4, 
                      zorder=4)

        # Label ALL nodes with enhanced styling
        labels = nx.get_node_attributes(G, 'label')
        label_fontsize = calculate_optimal_font_size(node_count, fig_size, 10)
        
        for node_id in G.nodes():
            if node_id in pos:
                x, y = pos[node_id]
                importance = node_importance.get(node_id, 0)
                
                # Adjust label style based on importance
                if importance > 0.7:  # High importance
                    bbox_style = dict(boxstyle='round,pad=0.5', 
                                    facecolor='white', 
                                    alpha=0.95, 
                                    edgecolor='#333333', 
                                    linewidth=2)
                    font_weight = 'bold'
                elif importance > 0.4:  # Medium importance
                    bbox_style = dict(boxstyle='round,pad=0.4', 
                                    facecolor='white', 
                                    alpha=0.9, 
                                    edgecolor='#666666', 
                                    linewidth=1.5)
                    font_weight = 'semibold'
                else:  # Lower importance
                    bbox_style = dict(boxstyle='round,pad=0.3', 
                                    facecolor='white', 
                                    alpha=0.85, 
                                    edgecolor='#999999', 
                                    linewidth=1)
                    font_weight = 'normal'
                
                ax.annotate(labels[node_id], (x, y),
                           fontsize=label_fontsize, 
                           ha='center', va='center',
                           fontproperties=font_prop, 
                           zorder=5, 
                           weight=font_weight,
                           bbox=bbox_style)

        # Enhanced legend with better styling
        if num_communities > 1 and num_communities <= 10:
            legend_elements = []
            for i in range(num_communities):
                community_size = sum(1 for comm in community_map.values() if comm == i)
                legend_elements.append(plt.Line2D([0], [0], marker='o', color='w',
                                                markerfacecolor=community_colors[i],
                                                markersize=14,
                                                markeredgecolor='white',
                                                markeredgewidth=2,
                                                label=f'Community {i+1} ({community_size} members)'))

            legend = ax.legend(handles=legend_elements, 
                             loc='upper right',
                             bbox_to_anchor=(0.98, 0.98), 
                             fontsize=10,
                             frameon=True, 
                             fancybox=True, 
                             shadow=True,
                             prop=font_prop)
            legend.get_frame().set_facecolor('white')
            legend.get_frame().set_alpha(0.95)
            legend.get_frame().set_edgecolor('#CCCCCC')

        # Enhanced title and subtitle
        title_fontsize = calculate_optimal_font_size(len(guild.name), fig_size, 18)
        ax.set_title(f'{guild.name} - Member Relationship Network',
                    fontsize=title_fontsize, 
                    fontproperties=font_prop, 
                    pad=30, 
                    weight='bold',
                    color='#2E3440')

        subtitle = (f'Showing {len(G.nodes())} most connected members • '
                   f'Node size = importance • Colors = communities • '
                   f'Line thickness = relationship strength')
        ax.text(0.5, 0.02, subtitle, 
               transform=ax.transAxes, 
               ha='center', va='bottom',
               fontsize=10, 
               style='italic', 
               color='#666666',
               fontproperties=font_prop)

        # Add network statistics panel
        stats_text = []
        stats_text.append(f"Network Statistics:")
        stats_text.append(f"• Nodes: {len(G.nodes())}")
        stats_text.append(f"• Edges: {len(G.edges())}")
        stats_text.append(f"• Communities: {num_communities}")
        stats_text.append(f"• Avg. Connection: {len(G.edges())*2/len(G.nodes()):.1f}")
        
        stats_str = "\n".join(stats_text)
        ax.text(0.02, 0.98, stats_str, 
               transform=ax.transAxes,
               verticalalignment='top', 
               horizontalalignment='left',
               bbox=dict(boxstyle='round,pad=0.5', 
                        facecolor='white',
                        alpha=0.95, 
                        edgecolor='#CCCCCC'),
               fontsize=9, 
               fontfamily='monospace',
               fontproperties=font_prop)

        ax.axis('off')

        # Set axis limits with proper margins
        if node_x and node_y:
            x_range = max(node_x) - min(node_x)
            y_range = max(node_y) - min(node_y)
            x_margin = max(x_range * 0.3, 1.0)
            y_margin = max(y_range * 0.3, 1.0)
            ax.set_xlim(min(node_x) - x_margin, max(node_x) + x_margin)
            ax.set_ylim(min(node_y) - y_margin, max(node_y) + y_margin)

        fig.tight_layout(pad=2.0)
        
        return save_plot_to_buffer(fig, dpi=200)


def create_enhanced_community_colors(num_communities):
    """Create vibrant, distinct colors for communities with better contrast."""
    if num_communities <= 1:
        return ['#3498DB']  # Single vibrant blue
    
    # Predefined vibrant colors with good contrast
    vibrant_colors = [
        '#E74C3C',  # Red
        '#3498DB',  # Blue  
        '#2ECC71',  # Green
        '#F39C12',  # Orange
        '#9B59B6',  # Purple
        '#1ABC9C',  # Turquoise
        '#E67E22',  # Carrot
        '#34495E',  # Dark Blue Gray
        '#F1C40F',  # Yellow
        '#E91E63',  # Pink
        '#00BCD4',  # Cyan
        '#FF5722'   # Deep Orange
    ]
    
    if num_communities <= len(vibrant_colors):
        return vibrant_colors[:num_communities]
    
    # For more communities, use colormap
    import matplotlib.cm as cm
    colors = cm.Set3(np.linspace(0, 1, num_communities))
    return [f'#{int(r*255):02x}{int(g*255):02x}{int(b*255):02x}' for r, g, b, _ in colors]

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