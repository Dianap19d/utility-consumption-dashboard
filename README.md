# Utility Consumption Dashboard

An interactive, **single-file** dashboard for daily utility KPIs (electricity, water, gas, thermal)
across ~94 public institutions in northern Moldova, with outside-temperature context.

Filter by **building** and **date range**, switch granularity (daily / weekly / monthly), and explore
consumption trends, rankings, per-building totals, and how each utility correlates with the weather.

> The built [`index.html`](index.html) is fully self-contained (charts library + data are embedded),
> works offline, and can be opened by double-clicking — no server required.

---

## Features

- **KPI cards** per metric — period total, daily average, peak day, and trend vs. the previous equal period.
- **Daily consumption time series** with a moving average and an overlaid **outside-temperature** line (second axis).
- **Top-buildings ranking** and a **share-by-building** breakdown for the focus metric.
- **Consumption vs. temperature scatter** with a least-squares trend line, Pearson *r*, R², and a plain-language read.
- **Per-building totals** table, sortable by any metric.
- **Filters:** searchable building multi-select, date range with `7d / 30d / 90d / All` presets, and a day/week/month toggle.

## Tech stack

- Plain HTML/CSS/JS — no framework, no build toolchain.
- [Apache ECharts](https://echarts.apache.org/) for charts (vendored locally for offline use).
- Python ([openpyxl](https://openpyxl.readthedocs.io/)) for the data-cleaning pipeline.

## Quick start

```bash
pip install -r requirements.txt
python3 build_data.py        # cleans inputs -> data.json -> index.html
open index.html              # or just double-click it
```

`build_data.py` regenerates both `data.json` and the standalone `index.html` from the source files.

### Publish on GitHub Pages

`index.html` is a static file, so once pushed you can enable **Settings → Pages → Deploy from branch**
and the dashboard is live at `https://<user>.github.io/<repo>/`.

## Data pipeline & cleaning

The raw readings are **cumulative meter totals** entered by hand, so they are noisy. `build_data.py`
turns them into trustworthy daily KPIs:

1. **Normalize** metric labels (fixes typos like `ElectrıcIty`, `GAS`, `Electrick`) and units; converts to a
   single base unit per metric (`MWh→kWh`, `Gcal→MWh`, …) and drops non-consumption units
   (kW demand, kVArh reactive, l/h flow).
2. **Group by building + metric** (serial numbers are ignored — they are inconsistently transcribed).
3. **Daily consumption = day-over-day difference** of the cumulative reading, with three layers of typo defense:
   - one robust reading per day (median of same-day readings),
   - **spike removal** for inserted-digit errors (e.g. `165921.4` typed as `1659214`),
   - a per-meter diff cap, plus a **global plausibility fence** at the 99th percentile × 6.
4. **Join** building names/addresses from the institutions workbook.
5. **Attach** daily outside temperature for the period.

### Data quality caveats

These are best-effort cleaned KPIs from messy manual data. Absolute totals are approximate, and buildings
with very sparse or garbled readings may be under-counted. **Trends and relative comparisons are reliable.**
The *Thermal* series in particular has little clean data — treat its correlation with caution.

## Project structure

| File | Role |
|------|------|
| `index.html` | Built, standalone dashboard (data + charts embedded). |
| `dashboard_template.html` | HTML/CSS/JS template with `__ECHARTS__` / `__DATA__` placeholders. |
| `build_data.py` | Cleaning pipeline; emits `data.json` and assembles `index.html`. |
| `data.json` | Cleaned daily-consumption + temperature export. |
| `echarts.min.js` | Vendored charts library (inlined at build time). |
| ` Date filtrate.csv` | Raw meter readings. |
| `Date prelucrate institutii corectate copy.xlsx` | Building code → name/address map. |
| `weather_raw.json` | Cached daily temperatures from Open-Meteo. |

## Data sources

- Meter readings and institution map: provided source files.
- Outside temperature (Bălți, Moldova): [Open-Meteo](https://open-meteo.com/) ERA5 archive — free for non-commercial use, CC-BY 4.0.

---

© 2026. All rights reserved.
