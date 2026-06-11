"""
RUSLE Pipeline — Step 1: Download all input data.

Downloads (all free, no account required except Planetary Computer sign-in):
  DEM        Copernicus GLO-30 tiles from AWS S3 (30 m)
  FlowAccum  Derived from DEM via pysheds D8 algorithm
  SoilGrids  ISRIC WCS — sand / silt / clay at 0-5 cm
  SM2RAIN    OpenLandMap v0.3 monthly climatology from Zenodo (1 km, COG)
  Landsat 8  Microsoft Planetary Computer STAC — B4+B5 median 2017
  LULC       ESA WorldCover 2020 from AWS S3 (10 m)

Safe to re-run — existing files are skipped automatically.
Run: python Scripts/01_download.py
"""

import os
import sys
import math
import shutil
import warnings
import tempfile

try:
    sys.stdout.reconfigure(encoding='utf-8')
except AttributeError:
    pass

import numpy as np
import requests
import rasterio
from rasterio.merge import merge as rio_merge
from rasterio.mask import mask as rio_mask
from rasterio.warp import reproject, Resampling
import geopandas as gpd
from shapely.geometry import box as shapely_box

sys.path.insert(0, os.path.dirname(__file__))
from config import BBOX, DIRS, TARGET_CRS, DEM_RES, DATE_START, DATE_END, catchments

warnings.filterwarnings('ignore')
TIMEOUT = 300


# ── Generic helpers ───────────────────────────────────────────────────────────

def _download(url, dest, label=''):
    if os.path.exists(dest):
        print(f'  [SKIP] {label or os.path.basename(dest)}')
        return False
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    print(f'  Downloading {label or os.path.basename(dest)} ...', end=' ', flush=True)
    r = requests.get(url, stream=True, timeout=TIMEOUT)
    r.raise_for_status()
    with open(dest, 'wb') as f:
        for chunk in r.iter_content(chunk_size=256 * 1024):
            f.write(chunk)
    print(f'{os.path.getsize(dest) / 1e6:.1f} MB')
    return True


def _clip_to_bbox(src_path, dst_path, bbox):
    west, south, east, north = bbox
    geom = [shapely_box(west, south, east, north).__geo_interface__]
    with rasterio.open(src_path) as src:
        arr, transform = rio_mask(src, geom, crop=True, all_touched=True,
                                  nodata=src.nodata, filled=True)
        meta = src.meta.copy()
    meta.update({'height': arr.shape[1], 'width': arr.shape[2],
                 'transform': transform, 'compress': 'lzw'})
    with rasterio.open(dst_path, 'w', **meta) as dst:
        dst.write(arr)


# ── 1. Copernicus DEM GLO-30 ──────────────────────────────────────────────────

def _cop_tile_url(lat, lon):
    ns = 'N' if lat >= 0 else 'S'
    ew = 'E' if lon >= 0 else 'W'
    name = f'Copernicus_DSM_COG_10_{ns}{abs(lat):02d}_00_{ew}{abs(lon):03d}_00_DEM'
    return f'https://copernicus-dem-30m.s3.amazonaws.com/{name}/{name}.tif'


def download_dem(bbox, out_dir):
    out_path = os.path.join(out_dir, 'dem_30m.tif')
    if os.path.exists(out_path):
        print('  [SKIP] DEM')
        return out_path

    west, south, east, north = bbox
    tile_paths = []
    for lat in range(int(math.floor(south)), int(math.floor(north)) + 1):
        for lon in range(int(math.floor(west)), int(math.floor(east)) + 1):
            url   = _cop_tile_url(lat, lon)
            fname = os.path.join(out_dir, f'cop_{lat:+03d}_{lon:+04d}.tif')
            try:
                _download(url, fname, label=f'CopDEM N{abs(lat):02d}E{abs(lon):03d}')
                tile_paths.append(fname)
            except requests.HTTPError as e:
                print(f'  [WARN] Tile N{abs(lat):02d}E{abs(lon):03d}: {e}')

    if not tile_paths:
        raise RuntimeError('No DEM tiles downloaded.')

    merged_path = out_path.replace('.tif', '_merged.tif')
    if len(tile_paths) == 1:
        shutil.copy2(tile_paths[0], merged_path)
    else:
        datasets = [rasterio.open(p) for p in tile_paths]
        merged, transform = rio_merge(datasets)
        meta = datasets[0].meta.copy()
        meta.update({'height': merged.shape[1], 'width': merged.shape[2],
                     'transform': transform, 'compress': 'lzw'})
        with rasterio.open(merged_path, 'w', **meta) as dst:
            dst.write(merged)
        for ds in datasets:
            ds.close()

    _clip_to_bbox(merged_path, out_path, bbox)
    os.remove(merged_path)
    print(f'  DEM saved  ({os.path.getsize(out_path)/1e6:.1f} MB)')
    return out_path


# ── 2. Flow accumulation via pysheds ─────────────────────────────────────────

def compute_flowaccum(dem_path, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, 'flowaccum_30m.tif')
    if os.path.exists(out_path):
        print('  [SKIP] Flow accumulation')
        return out_path

    try:
        import numpy as _np
        if not hasattr(_np, 'in1d'):
            _np.in1d = _np.isin
        from pysheds.grid import Grid
    except ImportError:
        print('  [ERROR] pysheds not installed — pip install pysheds')
        sys.exit(1)

    print('  Computing flow accumulation (pysheds) ...', end=' ', flush=True)
    grid = Grid.from_raster(dem_path)
    dem  = grid.read_raster(dem_path)
    acc  = grid.accumulation(grid.flowdir(grid.resolve_flats(
           grid.fill_depressions(grid.fill_pits(dem)))))
    grid.to_raster(acc, out_path, blockxsize=256, blockysize=256)
    print('done')
    return out_path


# ── 3. SoilGrids (ISRIC WCS) ─────────────────────────────────────────────────

_WCS = ('https://maps.isric.org/mapserv?map=/map/{prop}.map'
        '&SERVICE=WCS&VERSION=2.0.1&REQUEST=GetCoverage'
        '&COVERAGEID={prop}_{layer}&FORMAT=image/tiff'
        '&SUBSET=X({west},{east})&SUBSET=Y({south},{north})'
        '&SUBSETTINGCRS=http://www.opengis.net/def/crs/EPSG/0/4326')


def download_soilgrids(bbox, out_dir):
    west, south, east, north = bbox
    for prop in ('sand', 'silt', 'clay'):
        fname = f'{prop}_0-5cm_mean.tif'
        dest  = os.path.join(out_dir, fname)
        if os.path.exists(dest):
            print(f'  [SKIP] {fname}')
            continue
        url = _WCS.format(prop=prop, layer='0-5cm_mean',
                          west=west, east=east, south=south, north=north)
        _download(url, dest, label=fname)


# ── 4. SM2RAIN OpenLandMap v0.3 (Zenodo COG range requests) ──────────────────

_SM2RAIN_URL = (
    'https://zenodo.org/records/6458580/files/'
    'clm_precipitation_wc.v2.1.chelsa.v2.1.sm2rain.{month}'
    '_m_1km_s0..0cm_1980..2020_v0.3.tif'
)
_MONTHS = ['jan','feb','mar','apr','may','jun',
           'jul','aug','sep','oct','nov','dec']


def download_sm2rain(bbox, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    west, south, east, north = bbox
    geom = [shapely_box(west, south, east, north).__geo_interface__]
    print('  Fetching SM2RAIN from Zenodo (COG bbox clips) ...')
    for i, month in enumerate(_MONTHS, 1):
        out_path = os.path.join(out_dir, f'sm2rain_{i:02d}.tif')
        if os.path.exists(out_path):
            print(f'    [{i:02d}/12] {month}  [SKIP]', flush=True)
            continue
        url = _SM2RAIN_URL.format(month=month)
        print(f'    [{i:02d}/12] {month} ...', end=' ', flush=True)
        with rasterio.open(url) as src:
            arr, transform = rio_mask(src, geom, crop=True, all_touched=True,
                                      filled=True, nodata=src.nodata or -9999)
            meta = src.meta.copy()
            meta.update({'driver': 'GTiff', 'height': arr.shape[1],
                         'width': arr.shape[2], 'transform': transform,
                         'count': 1, 'dtype': 'float32',
                         'compress': 'lzw', 'nodata': -9999})
        with rasterio.open(out_path, 'w', **meta) as dst:
            dst.write(arr[0].astype('float32'), 1)
        print(f'{os.path.getsize(out_path)/1e3:.0f} kB', flush=True)


# ── 5. Landsat 8 (Planetary Computer STAC) ───────────────────────────────────

def download_landsat8(bbox, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, 'landsat8_2017_B4B5.tif')
    if os.path.exists(out_path):
        print('  [SKIP] Landsat 8')
        return out_path

    try:
        import pystac_client, planetary_computer
    except ImportError:
        print('  [ERROR] pip install pystac-client planetary-computer')
        sys.exit(1)

    cat_b = catchments.to_crs('EPSG:4326').total_bounds
    m = 0.1
    search_bbox = [cat_b[0]-m, cat_b[1]-m, cat_b[2]+m, cat_b[3]+m]

    print('  Searching Planetary Computer for Landsat 8 ...', end=' ', flush=True)
    catalog = pystac_client.Client.open(
        'https://planetarycomputer.microsoft.com/api/stac/v1',
        modifier=planetary_computer.sign_inplace,
    )
    search = catalog.search(
        collections=['landsat-c2-l2'], bbox=search_bbox,
        datetime=f'{DATE_START}/{DATE_END}',
        query={'eo:cloud_cover': {'lt': 30}, 'platform': {'in': ['landsat-8']}},
    )
    items = sorted(list(search.items()),
                   key=lambda i: i.properties.get('eo:cloud_cover', 100))
    if not items:
        print('[ERROR] No scenes found'); sys.exit(1)

    item = items[0]
    print(f'{item.id}  (cloud {item.properties.get("eo:cloud_cover","?")}%)')

    def _asset(item, *keys):
        for k in keys:
            if k in item.assets:
                return item.assets[k].href
        raise KeyError(keys)

    west, south, east, north = bbox
    bands = []
    out_meta = None
    for url, bname in [(_asset(item,'red','SR_B4','B4'), 'B4'),
                       (_asset(item,'nir08','SR_B5','B5'), 'B5')]:
        print(f'  Reading {bname} ...', end=' ', flush=True)
        clip_gdf = gpd.GeoDataFrame(
            geometry=[shapely_box(west, south, east, north)], crs='EPSG:4326'
        ).to_crs(rasterio.open(url).crs)
        with rasterio.open(url) as src:
            arr, transform = rio_mask(src,
                [g.__geo_interface__ for g in clip_gdf.geometry],
                crop=True, nodata=src.nodata or 0, filled=True)
            if out_meta is None:
                out_meta = src.meta.copy()
                out_meta.update({'count': 1, 'compress': 'lzw',
                                 'height': arr.shape[1], 'width': arr.shape[2],
                                 'transform': transform})
        bands.append(np.clip(arr[0].astype(np.float32) * 2.75e-5 - 0.2, 0, 1))
        print(f'{arr.shape[1]}×{arr.shape[2]} px', flush=True)

    out_meta.update({'count': 2, 'dtype': 'float32',
                     'nodata': -9999, 'compress': 'lzw'})
    with rasterio.open(out_path, 'w', **out_meta) as dst:
        dst.write(bands[0], 1)
        dst.write(bands[1], 2)
    print(f'  Landsat 8 saved  ({os.path.getsize(out_path)/1e6:.1f} MB)')
    return out_path


# ── 6. ESA WorldCover 2020 ────────────────────────────────────────────────────

def _wc_url(lat3, lon3):
    ns = 'N' if lat3 >= 0 else 'S'
    ew = 'E' if lon3 >= 0 else 'W'
    name = (f'ESA_WorldCover_10m_2020_v100_'
            f'{ns}{abs(lat3):02d}{ew}{abs(lon3):03d}_Map')
    return f'https://esa-worldcover.s3.amazonaws.com/v100/2020/map/{name}.tif'


def download_worldcover(bbox, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, 'worldcover_2020.tif')
    if os.path.exists(out_path):
        print('  [SKIP] ESA WorldCover')
        return out_path

    west, south, east, north = bbox
    lat_bases = sorted({int(math.floor(l/3)*3)
                        for l in range(int(math.floor(south)), int(math.ceil(north))+1)})
    lon_bases = sorted({int(math.floor(l/3)*3)
                        for l in range(int(math.floor(west)), int(math.ceil(east))+1)})

    tile_paths = []
    for lat3 in lat_bases:
        for lon3 in lon_bases:
            ns = 'N' if lat3 >= 0 else 'S'
            ew = 'E' if lon3 >= 0 else 'W'
            fname = os.path.join(out_dir, f'wc_{ns}{abs(lat3):02d}{ew}{abs(lon3):03d}.tif')
            try:
                _download(_wc_url(lat3, lon3), fname,
                          label=f'WorldCover {ns}{abs(lat3):02d}{ew}{abs(lon3):03d}')
                tile_paths.append(fname)
            except requests.HTTPError as e:
                print(f'  [WARN] {e}')

    if not tile_paths:
        raise RuntimeError('No WorldCover tiles downloaded.')

    if len(tile_paths) == 1:
        _clip_to_bbox(tile_paths[0], out_path, bbox)
    else:
        datasets = [rasterio.open(p) for p in tile_paths]
        merged, transform = rio_merge(datasets)
        meta = datasets[0].meta.copy()
        meta.update({'height': merged.shape[1], 'width': merged.shape[2],
                     'transform': transform, 'compress': 'lzw'})
        tmp = out_path + '.tmp.tif'
        with rasterio.open(tmp, 'w', **meta) as dst:
            dst.write(merged)
        for ds in datasets:
            ds.close()
        _clip_to_bbox(tmp, out_path, bbox)
        os.remove(tmp)

    print(f'  WorldCover saved  ({os.path.getsize(out_path)/1e6:.1f} MB)')
    return out_path


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print('=' * 58)
    print('  RUSLE — Step 1: Download Input Data')
    print('=' * 58)
    print(f'  Bbox: W={BBOX[0]}  S={BBOX[1]}  E={BBOX[2]}  N={BBOX[3]}')
    print()

    print('─── Copernicus DEM GLO-30 ───')
    dem_path = download_dem(BBOX, DIRS['dem'])

    print()
    print('─── Flow Accumulation (pysheds D8) ───')
    compute_flowaccum(dem_path, DIRS['flowaccum'])

    print()
    print('─── SoilGrids 0-5 cm (ISRIC WCS) ───')
    download_soilgrids(BBOX, DIRS['soilgrids'])

    print()
    print('─── SM2RAIN v0.3 Monthly Precipitation (Zenodo) ───')
    download_sm2rain(BBOX, DIRS['sm2rain'])

    print()
    print('─── Landsat 8 C2 L2 2017 (Planetary Computer) ───')
    download_landsat8(BBOX, DIRS['landsat8'])

    print()
    print('─── ESA WorldCover 2020 (AWS S3) ───')
    download_worldcover(BBOX, DIRS['lulc'])

    print()
    print('All downloads complete.  Run 02_compute.py next.')


if __name__ == '__main__':
    main()
