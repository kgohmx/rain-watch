"""
Rain Watch SG — compares NEA's 2-hour forecast against real rain-gauge readings.

Why Python instead of a browser page: data.gov.sg's real-time API doesn't send
the CORS headers browsers require, so a webpage calling it directly gets
"Failed to fetch". Python's `requests` isn't a browser and isn't subject to
CORS, so it works reliably. This script fetches the data, prints a comparison,
appends a row to a CSV log (so you can study trends over time), and writes a
static HTML report you can open in any browser.

Usage:
    pip install requests
    python rain_watch.py

Run it again anytime (or schedule it, e.g. via cron / Task Scheduler every
15-30 min) to build up a history in rain_watch_log.csv.
"""

import csv
import json
import os
import webbrowser
from datetime import datetime

import requests

FORECAST_URL = "https://api-open.data.gov.sg/v2/real-time/api/two-hr-forecast"
RAINFALL_URL = "https://api-open.data.gov.sg/v2/real-time/api/rainfall"

LOG_PATH = "data/rain_watch_log.csv"
REPORT_PATH = "docs/index.html"

RADAR_LINKS = {
    "NEA / MSS official radar (50km)": "https://www.weather.gov.sg/weather-rain-area-50km/",
    "Zoom Earth (Singapore)": "https://zoom.earth/places/singapore/",
}


def get(d, *keys, default=None):
    """Try several possible key spellings (API has changed casing before)."""
    for k in keys:
        if isinstance(d, dict) and k in d:
            return d[k]
    return default


def fetch_forecast():
    r = requests.get(FORECAST_URL, timeout=15)
    r.raise_for_status()
    payload = r.json()
    data = payload.get("data", {})
    area_meta = get(data, "areaMetadata", "area_metadata", default=[])
    items = data.get("items", [])
    if not items:
        raise ValueError(f"No forecast items in response: {json.dumps(payload)[:500]}")
    latest = items[-1]

    meta_by_name = {}
    for a in area_meta:
        loc = get(a, "labelLocation", "label_location", default={})
        meta_by_name[a.get("name")] = loc

    forecasts = get(latest, "forecasts", "forecast", default=[])
    rows = []
    for f in forecasts:
        area = f.get("area")
        text = f.get("forecast")
        loc = meta_by_name.get(area, {})
        rows.append({
            "area": area,
            "forecast": text,
            "is_rain": bool(text) and any(w in text.lower() for w in
                                           ["rain", "shower", "thunder", "drizzle"]),
            "lat": loc.get("latitude"),
            "lon": loc.get("longitude"),
        })
    updated = get(latest, "updatedTimestamp", "updated_timestamp", "timestamp")
    return updated, rows


def fetch_rainfall():
    r = requests.get(RAINFALL_URL, timeout=15)
    r.raise_for_status()
    payload = r.json()
    data = payload.get("data", {})
    stations = data.get("stations", [])
    readings = data.get("readings", [])
    if not readings:
        raise ValueError(f"No rainfall readings in response: {json.dumps(payload)[:500]}")
    latest = readings[-1]

    station_meta = {s.get("id", s.get("deviceId")): s for s in stations}
    reading_list = get(latest, "data", "readings", default=[])

    rows = []
    for r_ in reading_list:
        sid = get(r_, "stationId", "station_id")
        val = r_.get("value")
        meta = station_meta.get(sid, {})
        loc = get(meta, "location", "labelLocation", default={})
        rows.append({
            "station": meta.get("name", sid),
            "value_mm": val,
            "is_wet": isinstance(val, (int, float)) and val > 0.2,
            "lat": loc.get("latitude"),
            "lon": loc.get("longitude"),
        })
    return latest.get("timestamp"), rows


def append_log(forecast_rows, rainfall_rows, forecast_time, rainfall_time):
    rain_forecast_count = sum(1 for r in forecast_rows if r["is_rain"])
    wet_gauge_count = sum(1 for r in rainfall_rows if r["is_wet"])
    os.makedirs(os.path.dirname(LOG_PATH) or ".", exist_ok=True)
    is_new = not os.path.exists(LOG_PATH)
    with open(LOG_PATH, "a", newline="") as f:
        w = csv.writer(f)
        if is_new:
            w.writerow(["logged_at", "forecast_updated", "rainfall_updated",
                        "areas_total", "areas_forecast_rain",
                        "gauges_total", "gauges_wet"])
        w.writerow([
            datetime.now().isoformat(timespec="seconds"),
            forecast_time, rainfall_time,
            len(forecast_rows), rain_forecast_count,
            len(rainfall_rows), wet_gauge_count,
        ])
    return rain_forecast_count, wet_gauge_count


def write_report(forecast_time, forecast_rows, rainfall_time, rainfall_rows):
    rain_forecast_count = sum(1 for r in forecast_rows if r["is_rain"])
    wet_gauge_count = sum(1 for r in rainfall_rows if r["is_wet"])

    def forecast_row_html(r):
        cls = "rain" if r["is_rain"] else ""
        return f'<div class="row"><span>{r["area"]}</span><span class="val {cls}">{r["forecast"]}</span></div>'

    def rainfall_row_html(r):
        cls = "wet" if r["is_wet"] else "dry"
        val = f'{r["value_mm"]:.1f} mm' if isinstance(r["value_mm"], (int, float)) else "—"
        return f'<div class="row"><span>{r["station"]}</span><span class="val {cls}">{val}</span></div>'

    radar_links_html = "".join(
        f'<a href="{url}" target="_blank" rel="noopener">{label} ↗</a>'
        for label, url in RADAR_LINKS.items()
    )

    html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<title>Rain Watch SG — Report</title>
<style>
  body {{ background:#0a1420; color:#dce6ea; font-family:-apple-system,system-ui,sans-serif; padding:30px; }}
  .wrap {{ max-width:820px; margin:0 auto; }}
  h1 {{ margin-bottom:4px; }}
  .sub {{ color:#7f9aab; font-family:monospace; font-size:13px; margin-bottom:24px; }}
  .summary {{ display:flex; gap:14px; margin-bottom:20px; flex-wrap:wrap; }}
  .stat {{ background:#0f1e2e; border:1px solid #1c3a4f; border-radius:10px; padding:14px 18px; flex:1; min-width:160px; }}
  .stat .num {{ font-family:monospace; font-size:24px; color:#37c9a1; font-weight:700; }}
  .stat .lbl {{ font-size:12px; color:#7f9aab; margin-top:2px; }}
  .grid {{ display:grid; grid-template-columns:1fr 1fr; gap:16px; margin-bottom:20px; }}
  .panel {{ background:#0f1e2e; border:1px solid #1c3a4f; border-radius:10px; padding:16px 18px; }}
  .panel h2 {{ font-size:12px; text-transform:uppercase; letter-spacing:1px; color:#7f9aab; margin:0 0 10px; }}
  .row {{ display:flex; justify-content:space-between; padding:6px 0; border-bottom:1px dashed #1c3a4f; font-size:13.5px; }}
  .row:last-child {{ border-bottom:none; }}
  .val {{ font-family:monospace; color:#7f9aab; }}
  .val.rain, .val.wet {{ color:#e8a24d; font-weight:600; }}
  .radar-links {{ display:flex; gap:10px; flex-wrap:wrap; margin-top:10px; }}
  .radar-links a {{ background:#37c9a1; color:#0a1420; text-decoration:none; padding:8px 14px; border-radius:7px; font-weight:700; font-size:13px; font-family:monospace; }}
  footer {{ color:#7f9aab; font-size:12px; font-family:monospace; line-height:1.7; border-top:1px solid #1c3a4f; padding-top:14px; margin-top:10px; }}
</style></head>
<body><div class="wrap">
  <h1>Rain Watch SG</h1>
  <div class="sub">generated {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} · forecast updated {forecast_time} · gauges updated {rainfall_time}</div>

  <div class="summary">
    <div class="stat"><div class="num">{len(forecast_rows)}</div><div class="lbl">Areas in forecast</div></div>
    <div class="stat"><div class="num">{rain_forecast_count}</div><div class="lbl">Forecast as rain / showers</div></div>
    <div class="stat"><div class="num">{wet_gauge_count}</div><div class="lbl">Gauges actually recording rain</div></div>
  </div>

  <div class="panel" style="margin-bottom:20px;">
    <h2>Live radar (visual ground truth)</h2>
    <div class="radar-links">{radar_links_html}</div>
  </div>

  <div class="grid">
    <div class="panel">
      <h2>2-hour forecast</h2>
      {"".join(forecast_row_html(r) for r in sorted(forecast_rows, key=lambda x: x["area"] or ""))}
    </div>
    <div class="panel">
      <h2>Rain gauge readings</h2>
      {"".join(rainfall_row_html(r) for r in sorted(rainfall_rows, key=lambda x: -(x["value_mm"] or 0)))}
    </div>
  </div>

  <footer>
    Data: data.gov.sg / NEA (Meteorological Service Singapore).<br>
    Log file: <b>{LOG_PATH}</b> — every run appends a row, so you can chart forecast-vs-actual agreement over time.<br>
    Re-run this script (<code>python rain_watch.py</code>) to refresh. Radar imagery itself isn't an open API, hence the links above for the true visual comparison.
  </footer>
</div></body></html>"""

    os.makedirs(os.path.dirname(REPORT_PATH) or ".", exist_ok=True)
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        f.write(html)


def main():
    print("Fetching 2-hour forecast...")
    forecast_time, forecast_rows = fetch_forecast()
    print(f"  updated: {forecast_time}, {len(forecast_rows)} areas")

    print("Fetching rain gauge readings...")
    rainfall_time, rainfall_rows = fetch_rainfall()
    print(f"  updated: {rainfall_time}, {len(rainfall_rows)} stations")

    rain_forecast_count, wet_gauge_count = append_log(
        forecast_rows, rainfall_rows, forecast_time, rainfall_time
    )
    write_report(forecast_time, forecast_rows, rainfall_time, rainfall_rows)

    print()
    print(f"Areas forecast as rain/showers: {rain_forecast_count} / {len(forecast_rows)}")
    print(f"Gauges currently recording rain: {wet_gauge_count} / {len(rainfall_rows)}")
    print()
    print(f"Report written to: {os.path.abspath(REPORT_PATH)}")
    print(f"Log appended to:   {os.path.abspath(LOG_PATH)}")

    if not os.environ.get("GITHUB_ACTIONS"):
        try:
            webbrowser.open(f"file://{os.path.abspath(REPORT_PATH)}")
        except Exception:
            pass


if __name__ == "__main__":
    main()
