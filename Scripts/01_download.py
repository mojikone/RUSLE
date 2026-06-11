"""
RUSLE Pipeline -- Step 1: Download all input data.

Downloads (all free, no account required except Planetary Computer sign-in):
  DEM        Copernicus GLO-30 tiles from AWS S3 (30 m)
  FlowAccum  Derived from DEM via pysheds D8 algorithm
  SoilGrids  ISRIC WCS -- sand / silt / clay at 0-5 cm
  SM2RAIN    OpenLandMap v0.3 monthly climatology from Zenodo (1 km, COG)
  Landsat 8  Microsoft Planetary Computer STAC -- B4+B5 median 2017
  LULC       ESA WorldCover 2020 from AWS S3 (10 m)

Parallelism
-----------
  All six sources download simultaneously (outer ThreadPoolExecutor).
  Within each source, tiles / bands / months also download in parallel.
  Flow accumulation starts the moment the DEM finishes, overlapping
  with remaining downloads.
  N_WORKERS = cpu_count (all cores).

Safe to re-run -- existing files are skipped automatically.
Run: python Scripts/01_download.py
"""

import os
import sys
import math
import shutil
import time
import threading
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    sys.stdout.reconfigure(encoding='utf-8')
except AttributeError:
    pass

import numpy as np
import requests
import rasterio
from rasterio.merge import merge as rio_merge
from rasterio.mask import mask as rio_mask
import geopandas as gpd
from shapely.geometry import box as shapely_box

sys.path.insert(0, os.path.dirname(__file__))
from config import BBOX, DIRS, TARGET_CRS, DEM_RES, DATE_START, DATE_END, catchments

warnings.filterwarnings('ignore')

TIMEOUT   = 300
N_WORKERS = os.cpu_count() or 8
_LOCK     = threading.Lock()       # serialise console output across threads


def _log(msg=''):
    with _LOCK:
        print(msg, flush=True)


# ── Generic helpers ───────────────────────────────────────────────────────────

def _download(url, dest, label=''):
    name = label or os.path.basename(dest)
    if os.path.exists(dest):
        _log(f'  [SKIP] {name}')
        return False
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    r = requests.get(url, stream=True, timeout=TIMEOUT)
    r.raise_for_status()
    with open(dest, 'wb') as f:
        for chunk in r.iter_content(chunk_size=256 * 1024):
            f.write(chunk)
    _log(f'  {name}  ({os.path.getsize(dest) / 1e6:.1f} MB)')
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
        _log('  [SKIP] DEM')
        return out_path

    os.makedirs(out_dir, exist_ok=True)
    west, south, east, north = bbox
    tile_coords = [
        (lat, lon)
        for lat in range(int(math.floor(south)), int(math.floor(north)) + 1)
        for lon in range(int(math.floor(west)),  int(math.floor(east))  + 1)
    ]

    def _dl_tile(lat, lon):
        url   = _cop_tile_url(lat, lon)
        fname = os.path.join(out_dir, f'cop_{lat:+03d}_{lon:+04d}.tif')
        try:
            _download(url, fname, label=f'CopDEM N{abs(lat):02d}E{abs(lon):03d}')
            return fname
        except requests.HTTPError as e:
            _log(f'  [WARN] DEM tile N{abs(lat):02d}E{abs(lon):03d}: {e}')
            return None

    nw = min(len(tile_coords), N_WORKERS)
    with ThreadPoolExecutor(max_workers=nw) as ex:
        futs       = [ex.submit(_dl_tile, lat, lon) for lat, lon in tile_coords]
        tile_paths = [r for r in (f.result() for f in as_completed(futs)) if r]

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
    _log(f'  DEM saved  ({os.path.getsize(out_path)/1e6:.1f} MB)')
    return out_path


# ── 2. Flow accumulation via pysheds ─────────────────────────────────────────

def compute_flowaccum(dem_path, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, 'flowaccum_30m.tif')
    if os.path.exists(out_path):
        _log('  [SKIP] Flow accumulation')
        return out_path

    try:
        import numpy as _np
        if not hasattr(_np, 'in1d'):
            _np.in1d = _np.isin
        from pysheds.grid import Grid
    except ImportError:
        _log('  [ERROR] pysheds not installed -- pip install pysheds')
        sys.exit(1)

    _log('  Computing flow accumulation (pysheds) ...')
    grid = Grid.from_raster(dem_path)
    dem  = grid.read_raster(dem_path)
    acc  = grid.accumulation(grid.flowdir(grid.resolve_flats(
           grid.fill_depressions(grid.fill_pits(dem)))))
    grid.to_raster(acc, out_path, blockxsize=256, blockysize=256)
    _log('  Flow accumulation done')
    return out_path


# ── 3. SoilGrids (ISRIC WCS) ─────────────────────────────────────────────────

_WCS = ('https://maps.isric.org/mapserv?map=/map/{prop}.map'
        '&SERVICE=WCS&VERSION=2.0.1&REQUEST=GetCoverage'
        '&COVERAGEID={prop}_{layer}&FORMAT=image/tiff'
        '&SUBSET=X({west},{east})&SUBSET=Y({south},{north})'
        '&SUBSETTINGCRS=http://www.opengis.net/def/crs/EPSG/0/4326')


def download_soilgrids(bbox, out_dir):
    west, south, east, north = bbox

    def _dl_prop(prop):
        fname = f'{prop}_0-5cm_mean.tif'
        dest  = os.path.join(out_dir, fname)
        url   = _WCS.format(prop=prop, layer='0-5cm_mean',
                             west=west, east=east, south=south, north=north)
        _download(url, dest, label=fname)

    with ThreadPoolExecutor(max_workers=3) as ex:
        futs = [ex.submit(_dl_prop, p) for p in ('sand', 'silt', 'clay')]
        for f in as_completed(futs):
            f.result()


# ── 4. SM2RAIN OpenLandMap v0.3 (Zenodo COG range requests) ──────────────────

_SM2RAIN_URL = (
    'https://zenodo.org/records/6458580/files/'
    'clm_precipitation_wc.v2.1.chelsa.v2.1.sm2rain.{month}'
    '_m_1km_s0..0cm_1980..2020_v0.3.tif'
)
_MONTHS = ['jan', 'feb', 'mar', 'apr', 'may', 'jun',
           'jul', 'aug', 'sep', 'oct', 'nov', 'dec']


def download_sm2rain(bbox, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    west, south, east, north = bbox
    geom = [shapely_box(west, south, east, north).__geo_interface__]

    def _dl_month(i, month):
        out_path = os.path.join(out_dir, f'sm2rain_{i:02d}.tif')
        if os.path.exists(out_path):
            _log(f'  [SKIP] SM2RAIN {month}')
            return
        url = _SM2RAIN_URL.format(month=month)
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
        _log(f'  SM2RAIN {month}  ({os.path.getsize(out_path)/1e3:.0f} kB)')

    with ThreadPoolExecutor(max_workers=12) as ex:
        futs = [ex.submit(_dl_month, i, m) for i, m in enumerate(_MONTHS, 1)]
        for f in as_completed(futs):
            f.result()


# ── 5. Landsat 8 (Planetary Computer STAC) ───────────────────────────────────

def download_landsat8(bbox, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, 'landsat8_2017_B4B5.tif')
    if os.path.exists(out_path):
        _log('  [SKIP] Landsat 8')
        return out_path

    try:
        import pystac_client, planetary_computer
    except ImportError:
        _log('  [ERROR] pip install pystac-client planetary-computer')
        sys.exit(1)

    cat_b       = catchments.to_crs('EPSG:4326').total_bounds
    m           = 0.1
    search_bbox = [cat_b[0]-m, cat_b[1]-m, cat_b[2]+m, cat_b[3]+m]

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
        _log('  [ERROR] No Landsat 8 scenes found')
        sys.exit(1)

    item = items[0]
    _log(f'  Landsat: {item.id}  (cloud {item.properties.get("eo:cloud_cover","?")}%)')

    def _asset(item, *keys):
        for k in keys:
            if k in item.assets:
                return item.assets[k].href
        raise KeyError(keys)

    url_b4 = _asset(item, 'red',   'SR_B4', 'B4')
    url_b5 = _asset(item, 'nir08', 'SR_B5', 'B5')

    # Get scene CRS once -- both bands share the same projection
    with rasterio.open(url_b4) as src:
        scene_crs = src.crs

    west, south, east, north = bbox
    clip_gdf   = gpd.GeoDataFrame(
        geometry=[shapely_box(west, south, east, north)], crs='EPSG:4326'
    ).to_crs(scene_crs)
    clip_geoms = [g.__geo_interface__ for g in clip_gdf.geometry]

    def _read_band(url, bname):
        with rasterio.open(url) as src:
            arr, transform = rio_mask(src, clip_geoms, crop=True,
                                      nodata=src.nodata or 0, filled=True)
            meta = src.meta.copy()
            meta.update({'count': 1, 'compress': 'lzw',
                         'height': arr.shape[1], 'width': arr.shape[2],
                         'transform': transform})
        _log(f'  Landsat {bname}: {arr.shape[1]}x{arr.shape[2]} px')
        return np.clip(arr[0].astype(np.float32) * 2.75e-5 - 0.2, 0, 1), meta

    # Read B4 and B5 simultaneously
    with ThreadPoolExecutor(max_workers=2) as ex:
        fut_b4          = ex.submit(_read_band, url_b4, 'B4')
        fut_b5          = ex.submit(_read_band, url_b5, 'B5')
        band4, out_meta = fut_b4.result()
        band5, _        = fut_b5.result()

    out_meta.update({'count': 2, 'dtype': 'float32', 'nodata': -9999, 'compress': 'lzw'})
    with rasterio.open(out_path, 'w', **out_meta) as dst:
        dst.write(band4, 1)
        dst.write(band5, 2)
    _log(f'  Landsat 8 saved  ({os.path.getsize(out_path)/1e6:.1f} MB)')
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
        _log('  [SKIP] ESA WorldCover')
        return out_path

    west, south, east, north = bbox
    lat_bases   = sorted({int(math.floor(l/3)*3)
                          for l in range(int(math.floor(south)), int(math.ceil(north))+1)})
    lon_bases   = sorted({int(math.floor(l/3)*3)
                          for l in range(int(math.floor(west)),  int(math.ceil(east))+1)})
    tile_coords = [(lat3, lon3) for lat3 in lat_bases for lon3 in lon_bases]

    def _dl_wc_tile(lat3, lon3):
        ns    = 'N' if lat3 >= 0 else 'S'
        ew    = 'E' if lon3 >= 0 else 'W'
        fname = os.path.join(out_dir, f'wc_{ns}{abs(lat3):02d}{ew}{abs(lon3):03d}.tif')
        try:
            _download(_wc_url(lat3, lon3), fname,
                      label=f'WorldCover {ns}{abs(lat3):02d}{ew}{abs(lon3):03d}')
            return fname
        except requests.HTTPError as e:
            _log(f'  [WARN] {e}')
            return None

    nw = min(len(tile_coords), N_WORKERS)
    with ThreadPoolExecutor(max_workers=nw) as ex:
        futs       = [ex.submit(_dl_wc_tile, lat3, lon3) for lat3, lon3 in tile_coords]
        tile_paths = [r for r in (f.result() for f in as_completed(futs)) if r]

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

    _log(f'  WorldCover saved  ({os.path.getsize(out_path)/1e6:.1f} MB)')
    return out_path


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    t0 = time.perf_counter()
    print('=' * 58)
    print(f'  RUSLE -- Step 1: Download Input Data  ({N_WORKERS} workers)')
    print('=' * 58)
    print(f'  Bbox: W={BBOX[0]}  S={BBOX[1]}  E={BBOX[2]}  N={BBOX[3]}')
    print('  All sources downloading simultaneously ...')
    print()

    errors = {}

    with ThreadPoolExecutor(max_workers=N_WORKERS) as ex:
        # Fire all downloads at once
        fut_dem = ex.submit(download_dem,        BBOX, DIRS['dem'])
        fut_sg  = ex.submit(download_soilgrids,  BBOX, DIRS['soilgrids'])
        fut_sm  = ex.submit(download_sm2rain,    BBOX, DIRS['sm2rain'])
        fut_l8  = ex.submit(download_landsat8,   BBOX, DIRS['landsat8'])
        fut_wc  = ex.submit(download_worldcover, BBOX, DIRS['lulc'])

        # Flow accum starts the moment DEM finishes
        try:
            dem_path = fut_dem.result()
            fut_fa   = ex.submit(compute_flowaccum, dem_path, DIRS['flowaccum'])
        except Exception as e:
            errors['DEM'] = str(e)
            fut_fa = None

        # Wait for all remaining downloads
        for name, fut in [('SoilGrids',  fut_sg),
                          ('SM2RAIN',    fut_sm),
                          ('Landsat8',   fut_l8),
                          ('WorldCover', fut_wc)]:
            try:
                fut.result()
            except Exception as e:
                errors[name] = str(e)

        if fut_fa:
            try:
                fut_fa.result()
            except Exception as e:
                errors['FlowAccum'] = str(e)

    if errors:
        print()
        for src, err in errors.items():
            print(f'  [ERROR] {src}: {err}')
        sys.exit(1)

    print()
    print(f'  All downloads complete in {time.perf_counter() - t0:.1f}s')
    print('  Run 02_compute.py next.')


if __name__ == '__main__':
    main()
