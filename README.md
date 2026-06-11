# RUSLE Sediment Yield Pipeline

Offline, fully-automated RUSLE (Revised Universal Soil Loss Equation) pipeline
that computes per-catchment soil loss, sediment delivery, and reservoir
sedimentation from publicly available remote-sensing data.

---

## Quick Start

```bat
run.bat setup   # create .venv and install packages (first time only)
run.bat all     # download → compute → maps
```

Or step by step:

```bat
run.bat 01      # download all input data
run.bat 02      # compute RUSLE factors + export CSV
run.bat 03      # generate maps
```

---

## Directory Structure

```
├── Data/
│   ├── SHP/            ← catchment polygons (user-provided, committed to git)
│   ├── DEM/            ← Copernicus GLO-30 DEM tiles  [downloaded]
│   ├── FlowAccum/      ← D8 flow accumulation from DEM  [computed]
│   ├── SoilGrids/      ← ISRIC sand/silt/clay 0-5 cm  [downloaded]
│   ├── SM2RAIN/        ← monthly precipitation climatology  [downloaded]
│   ├── Landsat8/       ← Landsat 8 B4+B5 reflectance 2017  [downloaded]
│   └── LULC/           ← ESA WorldCover 2020  [downloaded]
├── Output/
│   ├── CSV/            ← RUSLE_Results.csv
│   ├── Rasters/        ← R, K, LS, C, P, SoilLoss GeoTIFFs
│   └── Maps/           ← 7 PNG maps (factor maps + classification)
├── Scripts/
│   ├── config.py       ← all settings and parameters
│   ├── 01_download.py
│   ├── 02_compute.py
│   └── 03_maps.py
├── run.bat
├── requirements.txt
└── README.md
```

---

## Input Requirements

### Catchment Shapefile — `Data/SHP/Catchments.shp`

- Polygon shapefile in any projected CRS (re-projected internally as needed)
- Must contain a **`name`** column with unique catchment identifiers (e.g. `1`, `2`, `3`, `4`)
- To use a different study area: replace the shapefile and update `BBOX` / `BUFFER_DEG`
  in `Scripts/config.py` if the auto-computed bbox is insufficient

### Parameters — `Scripts/config.py`

| Parameter | Default | Description |
|-----------|---------|-------------|
| `TARGET_CRS` | `EPSG:32640` | Projected CRS for computation (UTM Zone 40N) |
| `TARGET_RES` | 250 m | RUSLE computation pixel size |
| `DEM_RES` | 30 m | DEM / LS factor pixel size |
| `BUFFER_DEG` | 0.3° | Download padding around catchments |
| `DATE_START / DATE_END` | 2017 | Year used for Landsat (C factor) |
| `SDR` | 0.10 | Sediment Delivery Ratio |
| `SEDIMENT_DENSITY` | 1.30 t m⁻³ | Bulk density of deposited sediment |
| `DAM_LIFE_CYCLE` | 50 yr | Reservoir design life for sedimentation estimate |

---

## Data Sources

| Layer | Source | Resolution | URL |
|-------|--------|-----------|-----|
| DEM | Copernicus GLO-30 | 30 m | AWS S3 `copernicus-dem-30m` |
| Flow Accum. | Derived from DEM via **pysheds** D8 | 30 m | — |
| Soil texture | ISRIC SoilGrids 0-5 cm (sand/silt/clay) | 250 m | `maps.isric.org` WCS |
| Precipitation | OpenLandMap SM2RAIN v0.3 monthly climatology | 1 km | Zenodo [10.5281/zenodo.6458580](https://doi.org/10.5281/zenodo.6458580) |
| Landsat imagery | Landsat 8 C2 L2 2017 median | 30 m | Microsoft Planetary Computer STAC |
| LULC | ESA WorldCover 2020 | 10 m | AWS S3 `esa-worldcover` |

All downloads are free; no API keys are required. The SM2RAIN data uses
HTTP range requests (COG), so only the study-area bbox is downloaded.

---

## RUSLE Factors

### R — Rainfall Erosivity (MJ mm ha⁻¹ h⁻¹ yr⁻¹)

SM2RAIN OpenLandMap v0.3 monthly climatology. Formula (Renard 1997, as
implemented in GEE OpenLandMap script with Pm = Pa):

```
R = 1.73 × 10^(1.5 × (log₁₀(Pm) − 0.08188))
```

where **Pm** is the long-term mean monthly precipitation (mm/month).

### K — Soil Erodibility (t ha h ha⁻¹ MJ⁻¹ mm⁻¹)

Williams (1995) formula from SoilGrids ISRIC sand/silt/clay fractions at 0-5 cm:

```
K = (0.2 + 0.3 × exp(−0.0256 × SAN × (1 − SIL/100)))
  × (1 − (0.25 × CLA) / (CLA + exp(3.72 − 2.95 × CLA)))
```

### LS — Slope-Length Factor (dimensionless)

Moore & Burch (1986) using Copernicus DEM and pysheds D8 flow accumulation:

```
LS = 1.4 × (FA × 30 / 22.13)^0.4 × (sin(S) / 0.0896)^1.3
```

where **FA** = flow accumulation (cells, capped at 300), **S** = slope angle (°, capped at 30°).

### C — Cover Management Factor (dimensionless)

Durigon (2014) from Landsat 8 NDVI (2017 median composite, cloud < 20%):

```
C = 0.1 × (1 − NDVI) / 2
```

### P — Support Practice Factor (dimensionless)

Based on ESA WorldCover 2020 land-use class and terrain slope:

| LULC class | P |
|---|---|
| Natural vegetation (trees, shrubs, moss) | 0.8 |
| Cropland, slope < 5% | 0.5 |
| Cropland, slope 5–20% | 0.6–0.9 |
| All other classes (bare, urban, water) | 1.0 |

---

## Output

### `Output/CSV/RUSLE_Results.csv`

One row per catchment. Columns:

| Column | Unit | Description |
|--------|------|-------------|
| Name | — | Catchment identifier |
| R | MJ mm ha⁻¹ h⁻¹ yr⁻¹ | Rainfall erosivity |
| K | t ha h ha⁻¹ MJ⁻¹ mm⁻¹ | Soil erodibility |
| LS | — | Slope-length factor |
| C | — | Cover management factor |
| P | — | Support practice factor |
| Mean Soil Loss | t ha⁻¹ yr⁻¹ | A = R × K × LS × C × P |
| SDR | — | Sediment Delivery Ratio |
| Sediment Density | t m⁻³ | Bulk density of deposited material |
| Sediment Delivered | m³ km⁻² yr⁻¹ | `round(SoilLoss × 100 / Density × SDR, 0)` |
| Catchment Area | km² | Computed from shapefile (UTM projected) |
| Dam Life Cycle | yr | Reservoir design life |
| Sediment in N years | Mm³ | `Delivered × Area × LifeCycle / 1 000 000` |

### `Output/Rasters/`

GeoTIFF files at 250 m resolution (UTM 32640): `R.tif`, `K.tif`, `LS.tif`,
`C.tif`, `P.tif`, `SoilLoss.tif`.

### `Output/Maps/`

7 PNG maps at 200 DPI:

| File | Description |
|------|-------------|
| `R.png` | Rainfall erosivity |
| `K.png` | Soil erodibility |
| `LS.png` | Slope-length factor |
| `C.png` | Cover management factor |
| `P.png` | Support practice factor |
| `SoilLoss.png` | Annual soil loss (continuous scale) |
| `SoilLoss_Class.png` | Soil loss classification (5 classes) |

Soil loss classification:

| Class | Range | Colour |
|-------|-------|--------|
| Slight | < 5 t ha⁻¹ yr⁻¹ | Blue |
| Moderate | 5–10 | Cyan |
| High | 10–20 | Green |
| Very High | 20–40 | Yellow |
| Severe | > 40 | Red |

---

## Requirements

Python 3.10+ with packages listed in `requirements.txt`.
Install with:

```bat
run.bat setup
```

or manually:

```bash
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
```

---

## Adapting to a New Study Area

1. Replace `Data/SHP/Catchments.shp` with your catchment polygons
   (must have a `name` column)
2. Open `Scripts/config.py` and update:
   - `TARGET_CRS` if your area is in a different UTM zone
   - `DATE_START / DATE_END` for a different Landsat year
   - `SDR`, `SEDIMENT_DENSITY`, `DAM_LIFE_CYCLE` as needed
3. Run `run.bat all`

---

## References

- Renard, K.G. et al. (1997). *Predicting Soil Erosion by Water.* USDA Agricultural Handbook 703.
- Williams, J.R. (1995). *The EPIC model.* In: Singh, V.P. (ed.) Computer Models of Watershed Hydrology.
- Moore, I.D. & Burch, G.J. (1986). Modelling erosion and deposition: topographic effects. *Trans. ASAE* 29(6).
- Durigon, V.L. et al. (2014). NDVI time series for monitoring RUSLE cover factor. *Int. J. Remote Sens.* 35(2).
- Hengl, T. & Wheeler, I. (2018). *Monthly precipitation based on SM2RAIN-ASCAT.* Zenodo. doi:10.5281/zenodo.6458580
