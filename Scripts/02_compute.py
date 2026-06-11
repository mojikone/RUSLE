"""
RUSLE Pipeline — Step 2: Compute Factors & Output CSV.

Factors
-------
  R   Rainfall Erosivity   SM2RAIN OpenLandMap v0.3 monthly climatology
                           Formula (GEE-matching): R = 1.73 × 10^(1.5 × (log10(Pm) − 0.08188))
  K   Soil Erodibility     SoilGrids ISRIC 0-5 cm  — Williams (1995)
  LS  Slope-Length         Copernicus DEM 30 m + pysheds D8 flow accum
                           Moore & Burch (1986): 1.4 × (FA×30/22.13)^0.4 × (sin S/0.0896)^1.3
  C   Cover Management     Landsat 8 NDVI 2017 — Durigon (2014)
  P   Support Practice     ESA WorldCover 2020 LULC + slope

Output CSV columns
------------------
  Name, R, K, LS, C, P,
  Mean Soil Loss (t ha⁻¹ yr⁻¹),
  SDR,
  Sediment Density (t m⁻³),
  Sediment Delivered (m³ km⁻² yr⁻¹),
  Catchment Area (km²),
  Dam Life Cycle (yr),
  Sediment in N years (Mm³)

Parallelism: ThreadPoolExecutor for loading, factor computation, saving,
             and zonal statistics.

Run: python Scripts/02_compute.py
"""

import os
import sys
import csv
import glob
import time
import warnings

try:
    sys.stdout.reconfigure(encoding='utf-8')
except AttributeError:
    pass

import numpy as np
import rasterio
from rasterio.warp import reproject, Resampling
from rasterio.features import geometry_mask as rio_geom_mask
from rasterio.transform import from_origin
from shapely.geometry import mapping
from pyproj import Transformer
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(__file__))
from config import (
    DIRS, TARGET_CRS, TARGET_RES, DEM_RES, BBOX, catchments,
    SDR, SEDIMENT_DENSITY, DAM_LIFE_CYCLE,
)

warnings.filterwarnings('ignore')
N_WORKERS = max(1, (os.cpu_count() or 4) - 1)


# ── Fixed reference grids ─────────────────────────────────────────────────────

def _make_profile(res):
    tr = Transformer.from_crs('EPSG:4326', TARGET_CRS, always_xy=True)
    w, s = tr.transform(BBOX[0], BBOX[1])
    e, n = tr.transform(BBOX[2], BBOX[3])
    return {
        'driver': 'GTiff', 'dtype': 'float32', 'crs': TARGET_CRS,
        'transform': from_origin(w, n, res, res),
        'width': int(np.ceil((e - w) / res)),
        'height': int(np.ceil((n - s) / res)),
        'count': 1, 'nodata': -9999,
    }

TARGET_PROFILE = _make_profile(TARGET_RES)
DEM_PROFILE    = _make_profile(DEM_RES)


# ── Raster helpers ────────────────────────────────────────────────────────────

def reproject_to_grid(src_path, dst_profile, resampling=Resampling.bilinear):
    H, W = dst_profile['height'], dst_profile['width']
    with rasterio.open(src_path) as src:
        n = src.count
        out = np.full((n, H, W), np.nan, dtype=np.float32)
        for b in range(1, n + 1):
            reproject(
                source=rasterio.band(src, b), destination=out[b - 1],
                src_transform=src.transform, src_crs=src.crs,
                dst_transform=dst_profile['transform'], dst_crs=dst_profile['crs'],
                src_nodata=src.nodata if src.nodata is not None else -9999,
                dst_nodata=np.nan, resampling=resampling,
            )
    return (out[0], dst_profile) if n == 1 else (out, dst_profile)


def arr_to_target(arr, src_profile):
    H, W = TARGET_PROFILE['height'], TARGET_PROFILE['width']
    out  = np.full((H, W), np.nan, dtype=np.float32)
    reproject(
        source=np.where(np.isfinite(arr), arr, -9999).astype(np.float32),
        destination=out,
        src_transform=src_profile['transform'], src_crs=src_profile['crs'],
        dst_transform=TARGET_PROFILE['transform'], dst_crs=TARGET_PROFILE['crs'],
        src_nodata=-9999, dst_nodata=np.nan, resampling=Resampling.bilinear,
    )
    return out, TARGET_PROFILE


def save_raster(arr, profile, path):
    p = {**profile, 'count': 1, 'dtype': 'float32', 'nodata': -9999, 'compress': 'lzw'}
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with rasterio.open(path, 'w', **p) as dst:
        dst.write(np.where(np.isfinite(arr), arr, -9999).astype(np.float32), 1)


def zonal_mean(arr, profile, geometry):
    inside = rio_geom_mask(
        [mapping(geometry)],
        out_shape=(profile['height'], profile['width']),
        transform=profile['transform'],
        all_touched=True, invert=True,
    )
    valid = arr[inside & np.isfinite(arr)]
    return float(np.nanmean(valid)) if len(valid) else np.nan


# ── RUSLE factor formulas ─────────────────────────────────────────────────────

def compute_R(sm2rain_arrays):
    """GEE-matching formula: Pa = Pm → R = 1.73 × 10^(1.5 × (log10(Pm) − 0.08188))"""
    Pm = np.nanmean(np.stack(sm2rain_arrays, axis=0), axis=0)
    with np.errstate(divide='ignore', invalid='ignore'):
        R = 1.73 * 10 ** (1.5 * (np.log10(np.where(Pm > 0, Pm, np.nan)) - 0.08188))
    return np.where(np.isfinite(R), R, np.nan)


def compute_K(sand, silt, clay):
    SAN, SIL, CLA = sand / 10, silt / 10, clay / 10
    with np.errstate(over='ignore', invalid='ignore'):
        K = ((0.2 + 0.3 * np.exp(-0.0256 * SAN * (1 - SIL / 100))) *
             (1 - (0.25 * CLA) / (CLA + np.exp(3.72 - 2.95 * CLA))))
    return np.where(np.isfinite(K), K, np.nan)


def compute_LS(flowaccum, slope_deg, cell_size=DEM_RES):
    fa = np.minimum(flowaccum, 300.0)
    sc = np.minimum(slope_deg, 30.0)
    with np.errstate(invalid='ignore'):
        LS = 1.4 * ((fa * cell_size / 22.13) ** 0.4) * ((np.sin(np.radians(sc)) / 0.0896) ** 1.3)
    return np.where(np.isfinite(LS), LS, np.nan)


def compute_C(b4, b5):
    with np.errstate(invalid='ignore'):
        denom = np.where((b5 + b4) != 0, b5 + b4, np.nan)
        C = 0.1 * ((-((b5 - b4) / denom) + 1) / 2)
    return np.clip(np.where(np.isfinite(C), C, np.nan), 0, None)


def compute_P(lulc, slope_pct):
    lulc = lulc.astype(int)
    P    = np.ones_like(lulc, dtype=np.float32)
    veg  = np.isin(lulc, [10, 20, 30, 95])
    agri = np.isin(lulc, [40])
    P = np.where(veg,                                           0.8, P)
    P = np.where(agri & (slope_pct <   2),                     0.6, P)
    P = np.where(agri & (slope_pct >=  2) & (slope_pct <  5), 0.5, P)
    P = np.where(agri & (slope_pct >=  5) & (slope_pct <  8), 0.5, P)
    P = np.where(agri & (slope_pct >=  8) & (slope_pct < 12), 0.6, P)
    P = np.where(agri & (slope_pct >= 12) & (slope_pct < 16), 0.7, P)
    P = np.where(agri & (slope_pct >= 16) & (slope_pct < 20), 0.8, P)
    P = np.where(agri & (slope_pct >= 20),                     0.9, P)
    return P


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    t_total = time.perf_counter()
    print('=' * 60)
    print(f'  RUSLE — Step 2: Compute Factors')
    print(f'  Workers : {N_WORKERS}    Grid: {TARGET_PROFILE["width"]}×{TARGET_PROFILE["height"]} px @ {TARGET_RES} m')
    print('=' * 60, flush=True)

    FILES = {
        'dem':      os.path.join(DIRS['dem'],       'dem_30m.tif'),
        'fa':       os.path.join(DIRS['flowaccum'], 'flowaccum_30m.tif'),
        'sand':     os.path.join(DIRS['soilgrids'], 'sand_0-5cm_mean.tif'),
        'silt':     os.path.join(DIRS['soilgrids'], 'silt_0-5cm_mean.tif'),
        'clay':     os.path.join(DIRS['soilgrids'], 'clay_0-5cm_mean.tif'),
        'landsat8': os.path.join(DIRS['landsat8'],  'landsat8_2017_B4B5.tif'),
        'lulc':     os.path.join(DIRS['lulc'],      'worldcover_2020.tif'),
    }
    sm2rain_files = sorted(glob.glob(os.path.join(DIRS['sm2rain'], 'sm2rain_*.tif')))

    missing = [k for k, v in FILES.items() if not os.path.exists(v)]
    if missing or len(sm2rain_files) < 12:
        for k in missing:
            print(f'  [ERROR] Missing: {FILES[k]}')
        if len(sm2rain_files) < 12:
            print(f'  [ERROR] SM2RAIN: {len(sm2rain_files)}/12 files')
        print('  Run 01_download.py first.')
        sys.exit(1)

    # 1. Load all rasters in parallel ─────────────────────────────────────────
    print(f'\n[1/6] Loading {len(FILES) + len(sm2rain_files)} rasters ...', flush=True)
    t0 = time.perf_counter()

    load_tasks = {
        'dem':      dict(src_path=FILES['dem'],      dst_profile=DEM_PROFILE),
        'fa':       dict(src_path=FILES['fa'],       dst_profile=DEM_PROFILE),
        'sand':     dict(src_path=FILES['sand'],     dst_profile=TARGET_PROFILE),
        'silt':     dict(src_path=FILES['silt'],     dst_profile=TARGET_PROFILE),
        'clay':     dict(src_path=FILES['clay'],     dst_profile=TARGET_PROFILE),
        'landsat8': dict(src_path=FILES['landsat8'], dst_profile=TARGET_PROFILE),
        'lulc':     dict(src_path=FILES['lulc'],     dst_profile=TARGET_PROFILE,
                         resampling=Resampling.nearest),
    }
    for i, fp in enumerate(sm2rain_files):
        load_tasks[f'sm_{i:02d}'] = dict(src_path=fp, dst_profile=TARGET_PROFILE)

    def _load(key, src_path, dst_profile, resampling=Resampling.bilinear):
        return key, *reproject_to_grid(src_path, dst_profile, resampling)

    loaded = {}
    with ThreadPoolExecutor(max_workers=N_WORKERS) as ex:
        futs = {ex.submit(_load, k, **kw): k for k, kw in load_tasks.items()}
        for fut in as_completed(futs):
            k, arr, prof = fut.result()
            loaded[k] = (arr, prof)

    dem_30m, dem_prof = loaded['dem']
    fa_30m,  _        = loaded['fa']
    sand,    _        = loaded['sand']
    silt,    _        = loaded['silt']
    clay,    _        = loaded['clay']
    l8,      _        = loaded['landsat8']
    lulc,    _        = loaded['lulc']
    b4, b5            = (l8[0], l8[1]) if l8.ndim == 3 else (None, None)
    sm2rain_arrs      = [loaded[f'sm_{i:02d}'][0] for i in range(len(sm2rain_files))]
    print(f'      done in {time.perf_counter() - t0:.1f}s', flush=True)

    # 2. Compute factors (overlapped) ─────────────────────────────────────────
    print('\n[2/6] Computing factors ...', flush=True)
    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=N_WORKERS) as ex:
        fut_R = ex.submit(compute_R, sm2rain_arrs)
        fut_K = ex.submit(compute_K, sand, silt, clay)
        fut_C = ex.submit(compute_C, b4, b5)

        slope_deg = np.degrees(np.arctan(np.sqrt(sum(
            g**2 for g in np.gradient(dem_30m.astype(np.float64), DEM_RES)
        ))))
        slope_pct_30m = np.tan(np.radians(slope_deg)) * 100
        LS_30m        = compute_LS(fa_30m, slope_deg)

        fut_LS    = ex.submit(arr_to_target, LS_30m,        dem_prof)
        fut_slope = ex.submit(arr_to_target, slope_pct_30m, dem_prof)

        R  = fut_R.result()
        K  = fut_K.result()
        C  = fut_C.result()
        LS, _        = fut_LS.result()
        slope_pct, _ = fut_slope.result()

    P         = compute_P(lulc, slope_pct)
    soil_loss = R * K * LS * C * P
    print(f'      done in {time.perf_counter() - t0:.1f}s', flush=True)

    # 3. Save factor rasters ───────────────────────────────────────────────────
    print('\n[3/6] Saving rasters ...', flush=True)
    factors = {'R': R, 'K': K, 'LS': LS, 'C': C, 'P': P, 'SoilLoss': soil_loss}
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = {ex.submit(save_raster, arr, TARGET_PROFILE,
                          os.path.join(DIRS['output_rasters'], f'{name}.tif')): name
                for name, arr in factors.items()}
        for fut in as_completed(futs):
            print(f'      {futs[fut]}.tif', flush=True)

    # 4. Zonal statistics ─────────────────────────────────────────────────────
    print('\n[4/6] Zonal statistics ...', flush=True)
    cats      = catchments.to_crs(TARGET_CRS)
    cat_names = [str(r['name']) for _, r in cats.iterrows()]
    cat_geoms = [r.geometry for _, r in cats.iterrows()]
    cat_areas = [cats.iloc[i].geometry.area / 1e6 for i in range(len(cats))]

    zonal_tasks = [(ci, fn, factors[fn], TARGET_PROFILE, geom)
                   for ci, geom in enumerate(cat_geoms)
                   for fn in factors]
    results = [{} for _ in cat_names]
    with ThreadPoolExecutor(max_workers=N_WORKERS) as ex:
        futs = {ex.submit(zonal_mean, arr, prof, geom): (ci, fn)
                for ci, fn, arr, prof, geom in zonal_tasks}
        for fut in as_completed(futs):
            ci, fn = futs[fut]
            results[ci][fn] = fut.result()

    # 5. Print & build CSV rows ────────────────────────────────────────────────
    print('\n[5/6] Results:')
    hdr = (f"{'Name':<6} {'SoilLoss':>10} {'R':>8} {'K':>7} "
           f"{'LS':>7} {'C':>7} {'P':>6} {'Area km²':>9} {'Sed.Del.':>10} {'50yr Mm³':>10}")
    print('  ' + hdr)
    print('  ' + '-' * len(hdr))

    csv_rows = []
    for name, res, area in zip(cat_names, results, cat_areas):
        sl  = res.get('SoilLoss', np.nan)
        r   = res.get('R',        np.nan)
        k   = res.get('K',        np.nan)
        ls  = res.get('LS',       np.nan)
        c   = res.get('C',        np.nan)
        p   = res.get('P',        np.nan)
        # Sediment Delivered (m³/km²/yr) = round(SoilLoss × 100 / SedimentDensity × SDR, 0)
        sed_del = round(sl * 100 / SEDIMENT_DENSITY * SDR, 0) if np.isfinite(sl) else np.nan
        # Sediment in N years (Mm³)
        sed_50  = sed_del * area * DAM_LIFE_CYCLE / 1e6 if np.isfinite(sed_del) else np.nan

        print(f'  {name:<6} {sl:>10.3f} {r:>8.3f} {k:>7.4f} '
              f'{ls:>7.3f} {c:>7.4f} {p:>6.3f} {area:>9.4f} {sed_del:>10.0f} {sed_50:>10.6f}')

        csv_rows.append({
            'Name':                         name,
            'R':                            round(r,  3),
            'K':                            round(k,  4),
            'LS':                           round(ls, 3),
            'C':                            round(c,  4),
            'P':                            round(p,  3),
            'Mean Soil Loss (t/ha/yr)':     round(sl, 3),
            'SDR':                          SDR,
            'Sediment Density (t/m3)':      SEDIMENT_DENSITY,
            'Sediment Delivered (m3/km2/yr)': int(sed_del) if np.isfinite(sed_del) else '',
            'Catchment Area (km2)':         round(area, 4),
            'Dam Life Cycle (yr)':          DAM_LIFE_CYCLE,
            f'Sediment in {DAM_LIFE_CYCLE} years (Mm3)': round(sed_50, 6) if np.isfinite(sed_50) else '',
        })

    # 6. Save CSV ─────────────────────────────────────────────────────────────
    print('\n[6/6] Saving CSV ...', flush=True)
    csv_path = os.path.join(DIRS['output_csv'], 'RUSLE_Results.csv')
    fieldnames = list(csv_rows[0].keys())
    with open(csv_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(csv_rows)
    print(f'      Saved → {csv_path}')
    print(f'\nTotal time: {time.perf_counter() - t_total:.1f}s')
    print('Run 03_maps.py to generate maps.')


if __name__ == '__main__':
    main()
