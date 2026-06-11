"""
RUSLE Pipeline — Configuration
All paths are relative to the project root (parent of Scripts/).
Edit the PARAMETERS section to adapt to a new study area.
"""

import glob
import geopandas as gpd
import pandas as pd
from pathlib import Path

# ── Project root (resolved from this file's location) ────────────────────────
ROOT   = Path(__file__).resolve().parent.parent
DATA   = ROOT / 'Data'
OUTPUT = ROOT / 'Output'

# ── Input paths ───────────────────────────────────────────────────────────────
SHP_FILE = DATA / 'SHP' / 'Catchments.shp'

DIRS = {
    'shp':            DATA / 'SHP',
    'dem':            DATA / 'DEM',
    'flowaccum':      DATA / 'FlowAccum',
    'soilgrids':      DATA / 'SoilGrids',
    'sm2rain':        DATA / 'SM2RAIN',
    'landsat8':       DATA / 'Landsat8',
    'lulc':           DATA / 'LULC',
    'output_csv':     OUTPUT / 'CSV',
    'output_maps':    OUTPUT / 'Maps',
    'output_rasters': OUTPUT / 'Rasters',
}

for key in ('output_csv', 'output_maps', 'output_rasters'):
    DIRS[key].mkdir(parents=True, exist_ok=True)

DIRS = {k: str(v) for k, v in DIRS.items()}

# ── Catchments ────────────────────────────────────────────────────────────────
# Reads the 'name' column directly — no renaming.  Sorts by name ascending.
catchments = gpd.read_file(str(SHP_FILE))
catchments['name'] = catchments['name'].astype(str)
catchments = catchments.sort_values('name').reset_index(drop=True)

# ── Spatial settings ──────────────────────────────────────────────────────────
TARGET_CRS = 'EPSG:32640'   # UTM Zone 40N (metres)
TARGET_RES = 250            # RUSLE computation grid (m)
DEM_RES    = 30             # DEM / LS factor grid (m)
BUFFER_DEG = 0.3            # download padding around catchments (degrees)

_b = catchments.to_crs('EPSG:4326').total_bounds   # [xmin, ymin, xmax, ymax]
BBOX = (
    round(_b[0] - BUFFER_DEG, 6),
    round(_b[1] - BUFFER_DEG, 6),
    round(_b[2] + BUFFER_DEG, 6),
    round(_b[3] + BUFFER_DEG, 6),
)   # (west, south, east, north) WGS-84

# ── Date range (Landsat / C factor) ──────────────────────────────────────────
DATE_START = '2017-01-01'
DATE_END   = '2018-01-01'

# ── RUSLE sediment parameters (edit here to change assumptions) ───────────────
SDR              = 0.10    # Sediment Delivery Ratio          (dimensionless)
SEDIMENT_DENSITY = 1.30    # Bulk density of deposited material (t m⁻³)
DAM_LIFE_CYCLE   = 50      # Design life of reservoir          (years)

# ── Map colour palettes ───────────────────────────────────────────────────────
VIS = {
    'R': {
        'min': 0, 'max': 500,
        'palette': ['#0000ff', '#008000', '#ffff00', '#ffa500', '#ff0000'],
        'label':   'R factor  (MJ mm ha⁻¹ h⁻¹ yr⁻¹)',
        'title':   'Rainfall Erosivity  —  R Factor',
    },
    'K': {
        'min': 0.20, 'max': 0.25,
        'palette': ['#0000ff', '#008000', '#ffff00', '#ffa500', '#ff0000'],
        'label':   'K factor  (t ha h ha⁻¹ MJ⁻¹ mm⁻¹)',
        'title':   'Soil Erodibility  —  K Factor',
    },
    'LS': {
        'min': 0, 'max': 40,
        'palette': ['#a52508', '#ff3818', '#fbff18', '#25cdff', '#2f35ff', '#0b2dab'],
        'label':   'LS factor  (dimensionless)',
        'title':   'Slope–Length  —  LS Factor',
    },
    'C': {
        'min': 0, 'max': 0.10,
        'palette': ['#FFFFFF', '#CC9966', '#33CC00', '#006600'],
        'label':   'C factor  (dimensionless)',
        'title':   'Cover Management  —  C Factor',
    },
    'P': {
        'min': 0, 'max': 1,
        'palette': ['#FFFFFF', '#888888', '#000000'],
        'label':   'P factor  (dimensionless)',
        'title':   'Support Practice  —  P Factor',
    },
    'SoilLoss': {
        'min': 0, 'max': 50,
        'palette': ['#490eff', '#12f4ff', '#12ff50', '#e5ff12', '#ff4812'],
        'label':   'Soil Loss  (t ha⁻¹ yr⁻¹)',
        'title':   'Annual Soil Loss  —  RUSLE',
    },
}
