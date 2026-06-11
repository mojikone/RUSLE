"""
RUSLE Pipeline — Step 3: Generate Maps.

Produces 7 publication-quality PNGs:
  R, K, LS, C, P, SoilLoss, SoilLoss_Class

Each map:
  • Esri World Imagery satellite basemap (contextily, no API key)
  • Raster masked to catchment polygons (transparent outside)
  • Catchment boundaries + name labels
  • Colourbar, scale bar, north arrow
  • Clean title (no parenthetical notes)

Uses ProcessPoolExecutor — matplotlib is not thread-safe.
Run: python Scripts/03_maps.py
"""

import os
import sys
import time
import copy
import warnings
import math
from concurrent.futures import ProcessPoolExecutor, as_completed

try:
    sys.stdout.reconfigure(encoding='utf-8')
except AttributeError:
    pass

import numpy as np
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
from matplotlib.colors import LinearSegmentedColormap, Normalize, BoundaryNorm
import matplotlib.patheffects as pe
import rasterio
from rasterio.features import geometry_mask as rio_geom_mask
from shapely.geometry import mapping
import contextily as ctx

sys.path.insert(0, os.path.dirname(__file__))
from config import DIRS, VIS, catchments

warnings.filterwarnings('ignore')

FIG_SIZE = (10, 9)
DPI      = 200


# ── Helpers ───────────────────────────────────────────────────────────────────

def _cmap(palette, name='c'):
    return LinearSegmentedColormap.from_list(name, palette, N=256)


def _read_raster(factor_name):
    path = os.path.join(DIRS['output_rasters'], f'{factor_name}.tif')
    if not os.path.exists(path):
        raise FileNotFoundError(f'{path} — run 02_compute.py first')
    with rasterio.open(path) as src:
        arr       = src.read(1).astype(np.float32)
        nodata    = src.nodata if src.nodata is not None else -9999
        transform = src.transform
        crs       = src.crs
        extent    = [src.bounds.left, src.bounds.right,
                     src.bounds.bottom, src.bounds.top]
    return np.where(arr == nodata, np.nan, arr), transform, crs, extent


def _mask_to_catchments(arr, transform, crs):
    cats  = catchments.to_crs(crs)
    inside = rio_geom_mask(
        [mapping(g) for g in cats.geometry],
        out_shape=arr.shape, transform=transform,
        all_touched=True, invert=True,
    )
    return np.where(inside, arr, np.nan)


def _extent(crs, buffer_m=1500):
    b = catchments.to_crs(crs).total_bounds
    return b[0]-buffer_m, b[2]+buffer_m, b[1]-buffer_m, b[3]+buffer_m


def _scalebar(ax, xmin, xmax):
    raw = (xmax - xmin) * 0.20
    mag = 10 ** math.floor(math.log10(raw))
    nce = round(raw / mag) * mag
    x0, y0 = 0.05, 0.06
    x1 = x0 + nce / (xmax - xmin)
    kw = dict(xycoords='axes fraction', textcoords='axes fraction')
    ax.annotate('', xy=(x1, y0), xytext=(x0, y0), **kw,
                arrowprops=dict(arrowstyle='-', color='white', lw=2))
    ax.plot([x0,x0,x1,x1], [y0-.008,y0,y0,y0-.008],
            transform=ax.transAxes, color='white', lw=1.5, clip_on=False)
    lbl = f'{int(nce/1000)} km' if nce >= 1000 else f'{int(nce)} m'
    ax.text((x0+x1)/2, y0+.012, lbl, transform=ax.transAxes,
            ha='center', va='bottom', fontsize=9, color='white',
            path_effects=[pe.withStroke(linewidth=2, foreground='black')])


def _north(ax):
    ax.annotate('N', xy=(.93, .94), xytext=(.93, .88),
                xycoords='axes fraction', textcoords='axes fraction',
                ha='center', va='bottom', fontsize=11, fontweight='bold',
                color='white',
                path_effects=[pe.withStroke(linewidth=2, foreground='black')],
                arrowprops=dict(arrowstyle='->', color='white', lw=2))


def _basemap_and_limits(ax, crs):
    xmin, xmax, ymin, ymax = _extent(crs)
    ax.set_xlim(xmin, xmax)
    ax.set_ylim(ymin, ymax)
    ctx.add_basemap(ax, crs=crs.to_string(),
                    source=ctx.providers.Esri.WorldImagery,
                    zoom='auto', attribution_size=6, reset_extent=False)
    return xmin, xmax, ymin, ymax


def _overlay_catchments(ax, cats_proj):
    cats_proj.boundary.plot(ax=ax, color='white', linewidth=1.6, zorder=5)
    for _, row in cats_proj.iterrows():
        cx, cy = row.geometry.centroid.x, row.geometry.centroid.y
        ax.text(cx, cy, str(row.get('name', '')),
                fontsize=9, ha='center', va='center',
                color='white', fontweight='bold',
                path_effects=[pe.withStroke(linewidth=2.5, foreground='black')],
                zorder=6)


def _axes_style(ax, xmin, xmax):
    ax.xaxis.set_major_formatter(mticker.FuncFormatter(lambda v,_: f'{v/1000:.0f}'))
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v,_: f'{v/1000:.0f}'))
    ax.set_xlabel('Easting (km)', fontsize=11)
    ax.set_ylabel('Northing (km)', fontsize=11)
    ax.tick_params(labelsize=9)
    _scalebar(ax, xmin, xmax)
    _north(ax)


# ── Single-factor map ─────────────────────────────────────────────────────────

def make_map(factor_name):
    cfg = VIS[factor_name]
    arr, transform, crs, extent = _read_raster(factor_name)
    arr = _mask_to_catchments(arr, transform, crs)

    cm = copy.copy(_cmap(cfg['palette'], factor_name))
    cm.set_bad(alpha=0)
    norm = Normalize(vmin=cfg['min'], vmax=cfg['max'])

    fig, ax = plt.subplots(figsize=FIG_SIZE, dpi=DPI)
    ax.set_aspect('equal')
    xmin, xmax, ymin, ymax = _basemap_and_limits(ax, crs)

    im = ax.imshow(arr, cmap=cm, norm=norm, extent=extent,
                   origin='upper', interpolation='nearest',
                   aspect='equal', zorder=2, alpha=0.85)

    cats_proj = catchments.to_crs(crs)
    _overlay_catchments(ax, cats_proj)

    cbar = fig.colorbar(im, ax=ax, fraction=0.035, pad=0.02)
    cbar.set_label(cfg['label'], fontsize=11)
    cbar.ax.tick_params(labelsize=9)

    _axes_style(ax, xmin, xmax)
    ax.set_title(cfg['title'], fontsize=14, fontweight='bold', pad=12)

    plt.tight_layout()
    out = os.path.join(DIRS['output_maps'], f'{factor_name}.png')
    fig.savefig(out, dpi=DPI, bbox_inches='tight')
    plt.close(fig)
    print(f'  Saved {factor_name}.png', flush=True)


# ── Soil Loss classification map ──────────────────────────────────────────────

def make_soilloss_class():
    arr, transform, crs, extent = _read_raster('SoilLoss')
    arr = _mask_to_catchments(arr, transform, crs)

    breaks  = [0, 5, 10, 20, 40, 1e9]
    labels  = ['Slight  (<5)', 'Moderate  (5–10)',
               'High  (10–20)', 'Very High  (20–40)', 'Severe  (>40)']
    palette = ['#490eff', '#12f4ff', '#12ff50', '#e5ff12', '#ff4812']

    classified = np.full_like(arr, np.nan)
    for i, (lo, hi) in enumerate(zip(breaks[:-1], breaks[1:])):
        classified = np.where((arr >= lo) & (arr < hi), i + 1, classified)

    cm = copy.copy(LinearSegmentedColormap.from_list('sl', palette, N=5))
    cm.set_bad(alpha=0)

    fig, ax = plt.subplots(figsize=FIG_SIZE, dpi=DPI)
    ax.set_aspect('equal')
    xmin, xmax, ymin, ymax = _basemap_and_limits(ax, crs)

    ax.imshow(classified, cmap=cm, vmin=1, vmax=5, extent=extent,
              origin='upper', interpolation='nearest',
              aspect='equal', zorder=2, alpha=0.85)

    cats_proj = catchments.to_crs(crs)
    _overlay_catchments(ax, cats_proj)

    from matplotlib.patches import Patch
    ax.legend(
        handles=[Patch(facecolor=c, edgecolor='grey', label=l, alpha=0.85)
                 for c, l in zip(palette, labels)],
        title='Soil Loss (t ha⁻¹ yr⁻¹)', title_fontsize=9,
        loc='lower left', fontsize=9, framealpha=0.85,
    )

    _axes_style(ax, xmin, xmax)
    ax.set_title('Soil Loss Classification  —  RUSLE',
                 fontsize=14, fontweight='bold', pad=12)

    plt.tight_layout()
    out = os.path.join(DIRS['output_maps'], 'SoilLoss_Class.png')
    fig.savefig(out, dpi=DPI, bbox_inches='tight')
    plt.close(fig)
    print('  Saved SoilLoss_Class.png', flush=True)


# ── Process pool ──────────────────────────────────────────────────────────────

def _dispatch(task):
    try:
        make_soilloss_class() if task == 'SoilLoss_Class' else make_map(task)
        return task, None
    except Exception as e:
        return task, str(e)


def main():
    t0    = time.perf_counter()
    tasks = ['R', 'K', 'LS', 'C', 'P', 'SoilLoss', 'SoilLoss_Class']
    nw    = max(1, min(len(tasks), (os.cpu_count() or 4) - 1))
    print('=' * 58)
    print(f'  RUSLE — Step 3: Generate Maps  ({nw} processes)')
    print('=' * 58)

    with ProcessPoolExecutor(max_workers=nw) as ex:
        futs = {ex.submit(_dispatch, t): t for t in tasks}
        for fut in as_completed(futs):
            task, err = fut.result()
            print(f'  {"[FAIL]" if err else "Done :"} {task}' +
                  (f'  — {err}' if err else ''), flush=True)

    print(f'\nMaps saved to: {DIRS["output_maps"]}')
    print(f'Total time: {time.perf_counter() - t0:.1f}s')


if __name__ == '__main__':
    main()
