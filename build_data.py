#!/usr/bin/env python3
"""Clean meter readings + building map -> compact daily-consumption JSON for the dashboard."""
import csv, json, os, openpyxl
from collections import defaultdict
from datetime import datetime
import db_source

CSV = "./ Date filtrate.csv"
XLSX = "Date prelucrate institutii corectate copy.xlsx"
OUT = "data.json"

# --- building map: COD EMIS -> (name, address) ---
wb = openpyxl.load_workbook(XLSX, data_only=True)
ws = wb.active
bmap = {}
for i, r in enumerate(ws.iter_rows(values_only=True)):
    if i == 0 or not r[0]:
        continue
    bmap[str(r[0]).strip()] = (str(r[1] or "").strip(), str(r[2] or "").strip())

# --- metric / unit normalization. Each metric collapses to ONE base unit so we
#     never sum incompatible quantities. Non-consumption units (kW power demand,
#     kVArh reactive, l/h flow) are excluded from consumption KPIs. ---
DISPLAY_UNIT = {"Electricity": "kWh", "Gas": "m³", "Water": "m³", "Thermal": "MWh"}
METRICS = ["Electricity", "Water", "Gas", "Thermal"]

def norm_metric(m):
    s = (m or "").strip().lower()
    if s.startswith("electr"):
        return "Electricity"
    if s == "gas":
        return "Gas"
    if s == "water":
        return "Water"
    if s == "thermal":
        return "Thermal"
    return None

def factor(metric, unit):
    """Return multiplier to convert reading into the metric's base unit, or None to drop."""
    u = (unit or "").strip()
    if metric == "Electricity":
        return {"kWh": 1.0, "MWh": 1000.0, "Wh": 0.001}.get(u)  # base kWh; drop kW/kVArh
    if metric in ("Water", "Gas"):
        return 1.0 if u == "m3" else None  # base m3
    if metric == "Thermal":
        return {"MWh": 1.0, "Gcal": 1.163, "kWh": 0.001, "Wh": 1e-6}.get(u)  # base MWh
    return None

def parse_dt(s):
    s = (s or "").strip()
    if not s:
        return None
    try:
        return datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None

# serials are ignored (they are inconsistently transcribed). One series per
# building+metric:  meter[(code, metric)][day] = list of base-unit readings that day
meter = defaultdict(lambda: defaultdict(list))

# --- data source: Azure SQL (ems-dev-db) if .env credentials exist, else CSV ---
db_source.load_env()
if os.environ.get("DB_USER") and os.environ.get("DB_PASSWORD"):
    rows, table = db_source.fetch_rows()
    print(f"source    : Azure SQL {table} ({len(rows)} rows)")
else:
    rows = list(csv.DictReader(open(CSV, encoding="utf-8-sig")))
    print(f"source    : CSV {CSV} ({len(rows)} rows)")
used_codes = set()
for r in rows:
    metric = norm_metric(r["giz_captionraw"])
    if not metric:
        continue
    f = factor(metric, r["giz_valueunit"])
    if f is None:
        continue
    raw = r["giz_readingvalue"].strip()
    if not raw:
        continue
    try:
        val = float(raw) * f
    except ValueError:
        continue
    if val < 0:
        continue
    dt = parse_dt(r["giz_readingdatetime"])
    if dt is None:
        continue
    code = r["giz_institutioncode"].strip()
    meter[(code, metric)][dt.strftime("%Y-%m-%d")].append(val)
    used_codes.add(code)


def median(xs):
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def drop_spikes(vals):
    """Drop isolated transcription spikes: a point far above OR below BOTH neighbours
    (handles inserted digits like 70958 -> 709827, or 42605 -> 6926). Iterates to
    convergence. Scale-robust, so it tolerates legitimate slow meter growth."""
    UP, DOWN = 1.4, 0.7
    keep = list(vals)
    changed = True
    while changed and len(keep) >= 3:
        changed = False
        for i in range(len(keep)):
            lo = keep[i - 1] if i > 0 else None
            hi = keep[i + 1] if i < len(keep) - 1 else None
            refs = [x for x in (lo, hi) if x is not None and x > 0]
            if not refs:
                continue
            v = keep[i]
            up = all(v > UP * x for x in refs)
            dn = all(v < DOWN * x for x in refs)
            if (up or dn) and len(refs) == 2:      # only judge points with both neighbours
                del keep[i]
                changed = True
                break
    return keep

# --- daily consumption = positive day-over-day diff per meter, summed per building ---
# daily[code][metric][day] = consumption
daily = defaultdict(lambda: defaultdict(lambda: defaultdict(float)))
for (code, metric), byday in meter.items():
    days = sorted(byday)
    # one robust reading per day (median across same-day readings kills lone typos)
    series = [(d, median(byday[d])) for d in days]
    cleaned = drop_spikes([v for _, v in series])
    # re-pair cleaned values back to their days (spike removal preserves order)
    ci = 0
    kept = []
    for d, v in series:
        if ci < len(cleaned) and cleaned[ci] == v:
            kept.append((d, v))
            ci += 1
    # robust diff cap from the meter's own typical step
    diffs = [b[1] - a[1] for a, b in zip(kept, kept[1:]) if b[1] >= a[1]]
    cap = (median(diffs) * 12 + 1) if diffs else float("inf")
    prev = None
    for d, v in kept:
        if prev is not None:
            diff = v - prev
            if 0 <= diff <= cap:        # negative => reset/replacement; > cap => residual typo
                daily[code][metric][d] += diff
        prev = v

# --- global plausibility fence per metric ---
# Per-meter caps miss meters whose own history is mostly garbage (too few/too noisy
# points). Legit daily values and digit-insertion typos differ by ~100x, so a fence
# well above the 99th percentile removes the absurd ones without touching real spikes.
def quantile(a, p):
    a = sorted(a)
    if not a:
        return 0.0
    i = (len(a) - 1) * p
    lo = int(i)
    return a[lo] + (a[lo + 1] - a[lo]) * (i - lo) if lo + 1 < len(a) else a[lo]

for metric in METRICS:
    pool = [v for md in daily.values() if metric in md for v in md[metric].values() if v > 0]
    fence = quantile(pool, 0.99) * 6 if pool else float("inf")
    for code, md in daily.items():
        if metric in md:
            for d in [d for d, v in md[metric].items() if v > fence]:
                del md[metric][d]

all_days = sorted({d for c in daily.values() for m in c.values() for d in m})

buildings = []
for code in sorted(used_codes, key=lambda c: bmap.get(c, ("￿",))[0].lower()):
    name, addr = bmap.get(code, (code, ""))
    buildings.append({"code": code, "name": name or code, "address": addr})

# --- outside temperature (Bălți, northern Moldova) from Open-Meteo ERA5 archive,
#     cached in weather_raw.json. {date: mean_temp_C}. Graceful if file absent. ---
temp = {}
try:
    wj = json.load(open("weather_raw.json", encoding="utf-8"))["daily"]
    for d, t in zip(wj["time"], wj["temperature_2m_mean"]):
        if t is not None:
            temp[d] = round(t, 1)
except (FileNotFoundError, KeyError):
    pass

out = {
    "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
    "metrics": METRICS,
    "units": DISPLAY_UNIT,
    "dateMin": all_days[0] if all_days else None,
    "dateMax": all_days[-1] if all_days else None,
    "tempLocation": "Bălți, MD",
    "temp": temp,
    "buildings": buildings,
    # round to keep file small
    "daily": {c: {m: {d: round(v, 3) for d, v in dd.items()} for m, dd in md.items()}
              for c, md in daily.items()},
}
data_json = json.dumps(out, ensure_ascii=False, separators=(",", ":"))
with open(OUT, "w", encoding="utf-8") as fh:
    fh.write(data_json)

# --- assemble the standalone dashboard (template + vendored echarts + data) ---
import os
tpl = open("dashboard_template.html", encoding="utf-8").read()
echarts = open("echarts.min.js", encoding="utf-8").read()
html = tpl.replace("__ECHARTS__", echarts).replace("__DATA__", data_json)
assert "__ECHARTS__" not in html and "__DATA__" not in html
with open("index.html", "w", encoding="utf-8") as fh:
    fh.write(html)

print("buildings:", len(buildings), "| days:", len(all_days),
      "| range:", out["dateMin"], "->", out["dateMax"])
print("data.json :", round(os.path.getsize(OUT) / 1024, 1), "KB")
print("index.html:", round(os.path.getsize("index.html") / 1024 / 1024, 2), "MB (standalone, offline)")
