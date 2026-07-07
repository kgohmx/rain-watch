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
from datetime import datetime, timedelta, timezone

import requests

FORECAST_URL = "https://api-open.data.gov.sg/v2/real-time/api/two-hr-forecast"
RAINFALL_URL = "https://api-open.data.gov.sg/v2/real-time/api/rainfall"
LIGHTNING_URL = "https://api-open.data.gov.sg/v2/real-time/api/lightning"

LOG_PATH = "data/rain_watch_log.csv"
REPORT_PATH = "docs/index.html"

SGT = timezone(timedelta(hours=8))

# Radar images aren't exposed as a clean official API, but NEA's radar
# snapshots follow a predictable filename pattern (5-minute timestamps).
# We try a couple of known URL patterns and step back a few 5-min ticks
# in case the very latest one hasn't been published yet.
RADAR_PATTERNS = {
    "50km": "https://www.weather.gov.sg/files/rainarea/50km/v2/dpsri_70km_{ts}0000dBR.dpsri.png",
    "240km": "https://www.weather.gov.sg/files/rainarea/240km/dpsri_240km_{ts}0000dBR.dpsri.png",
}

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


def fetch_lightning():
    """Fetch recent lightning strikes. Returns (updated_time, strikes) where
    each strike has lat/lon/type/time. Best-effort: NEA's lightning feed
    schema isn't as stable as the others, so this fails soft."""
    r = requests.get(LIGHTNING_URL, timeout=15)
    r.raise_for_status()
    payload = r.json()
    data = payload.get("data", {})
    records = get(data, "records", "readings", "items", default=[])
    if not records:
        return None, []

    latest = records[-1]
    updated = get(latest, "datetime", "timestamp", "updatedTimestamp")
    readings = get(latest, "readings", "data", default=[])

    strikes = []
    for r_ in readings:
        # Lightning data is typically GeoJSON-ish: a FeatureCollection or a
        # flat list of {location, type, datetime}. Handle both shapes.
        features = r_.get("features") if isinstance(r_, dict) else None
        if features:
            for feat in features:
                coords = get(feat.get("geometry", {}), "coordinates", default=[None, None])
                props = feat.get("properties", {})
                strikes.append({
                    "lon": coords[0] if len(coords) > 0 else None,
                    "lat": coords[1] if len(coords) > 1 else None,
                    "type": props.get("type"),
                    "time": props.get("datetime") or props.get("time"),
                })
        elif isinstance(r_, dict) and ("lat" in r_ or "location" in r_):
            loc = get(r_, "location", default={})
            strikes.append({
                "lon": loc.get("longitude", r_.get("lon")),
                "lat": loc.get("latitude", r_.get("lat")),
                "type": r_.get("type"),
                "time": r_.get("datetime") or r_.get("time"),
            })
    return updated, strikes


def round_down_5min(dt):
    return dt.replace(minute=(dt.minute // 5) * 5, second=0, microsecond=0)


def download_radar_image(range_key, out_path, max_steps_back=4):
    """Try to fetch the current radar snapshot for a given range. Steps back
    in 5-minute increments if the very latest frame isn't published yet.
    Returns (success, frame_time_str) — never raises, so a bad guess about
    NEA's URL pattern doesn't break the rest of the report."""
    pattern = RADAR_PATTERNS[range_key]
    now = round_down_5min(datetime.now(SGT))
    for step in range(max_steps_back):
        ts = now - timedelta(minutes=5 * step)
        ts_str = ts.strftime("%Y%m%d%H%M")
        url = pattern.format(ts=ts_str)
        try:
            resp = requests.get(url, timeout=10)
            if resp.status_code == 200 and resp.headers.get("content-type", "").startswith("image"):
                os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
                with open(out_path, "wb") as f:
                    f.write(resp.content)
                return True, ts.strftime("%H:%M SGT")
        except requests.RequestException:
            continue
    return False, None


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


def write_report(forecast_time, forecast_rows, rainfall_time, rainfall_rows,
                  radar_status, lightning_time, strikes):
    rain_forecast_count = sum(1 for r in forecast_rows if r["is_rain"])
    wet_gauge_count = sum(1 for r in rainfall_rows if r["is_wet"])

    recent_strikes = []
    if strikes:
        cutoff = datetime.now(SGT) - timedelta(minutes=30)
        for s in strikes:
            t = s.get("time")
            try:
                st = datetime.fromisoformat(t.replace("Z", "+00:00")) if t else None
                if st and st.astimezone(SGT) >= cutoff:
                    recent_strikes.append(s)
            except (ValueError, AttributeError):
                recent_strikes.append(s)  # can't parse time, count it anyway

    def forecast_row_html(r):
        cls = "rain" if r["is_rain"] else ""
        return f'<div class="row"><span>{r["area"]}</span><span class="val {cls}">{r["forecast"]}</span></div>'

    def rainfall_row_html(r):
        cls = "wet" if r["is_wet"] else "dry"
        val = f'{r["value_mm"]:.1f} mm' if isinstance(r["value_mm"], (int, float)) else "—"
        return f'<div class="row"><span>{r["station"]}</span><span class="val {cls}">{val}</span></div>'

    # Build <option>s for the radar dropdown: embedded ranges first (only if
    # the image actually downloaded this run), then external fallback links.
    radar_options = []
    radar_images_html = []
    for key, (ok, frame_time) in radar_status.items():
        if ok:
            radar_options.append(f'<option value="radar_{key}">NEA radar — {key} ({frame_time})</option>')
            display = "block" if not radar_images_html else "none"
            radar_images_html.append(
                f'<img id="radar_{key}" src="radar_{key}.png" alt="NEA rain radar {key}" '
                f'style="display:{display};width:100%;border-radius:8px;border:1px solid #1c3a4f;">'
            )
    for label, url in RADAR_LINKS.items():
        safe_id = label.lower().replace(" ", "_").replace("/", "")
        radar_options.append(f'<option value="link_{safe_id}" data-url="{url}">{label} (opens new tab)</option>')

    has_embedded = any(ok for ok, _ in radar_status.values())
    radar_select_html = f"""
    <select id="radarSelect" onchange="onRadarChange()">
      {"".join(radar_options)}
    </select>
    <div id="radarImages">{"".join(radar_images_html) if radar_images_html else '<p style="color:#7f9aab;font-size:13px;">Live radar snapshot unavailable this run — use a link below instead.</p>'}</div>
    """

    lightning_banner = ""
    if recent_strikes:
        lightning_banner = f"""
    <div class="alert">⚡ {len(recent_strikes)} lightning strike(s) detected in the last 30 minutes.
    NEA advises suspending outdoor activities when lightning is nearby.</div>"""
    lightning_sub = f"{len(strikes)} strikes in latest reading" if strikes else "no strikes in latest reading"

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
  select {{ background:#10263a; color:#dce6ea; border:1px solid #1c3a4f; border-radius:7px; padding:8px 10px; font-family:monospace; font-size:13px; width:100%; margin-bottom:12px; }}
  .alert {{ background:#3a2410; border:1px solid #e8a24d; color:#f0c48a; border-radius:10px; padding:12px 16px; margin-bottom:18px; font-size:13.5px; font-weight:600; }}
  footer {{ color:#7f9aab; font-size:12px; font-family:monospace; line-height:1.7; border-top:1px solid #1c3a4f; padding-top:14px; margin-top:10px; }}
</style></head>
<body><div class="wrap">
  <h1>Rain Watch SG</h1>
  <div class="sub">generated {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} · forecast updated {forecast_time} · gauges updated {rainfall_time} · lightning: {lightning_sub}</div>

  {lightning_banner}

  <div class="summary">
    <div class="stat"><div class="num">{len(forecast_rows)}</div><div class="lbl">Areas in forecast</div></div>
    <div class="stat"><div class="num">{rain_forecast_count}</div><div class="lbl">Forecast as rain / showers</div></div>
    <div class="stat"><div class="num">{wet_gauge_count}</div><div class="lbl">Gauges actually recording rain</div></div>
  </div>

  <div class="panel" style="margin-bottom:20px;">
    <h2>Live radar</h2>
    {radar_select_html}
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
    Radar images are fetched from NEA's public file pattern, not an official documented API — if a range shows "unavailable," NEA likely changed something, use the link options instead.
  </footer>
</div>
<script>
function onRadarChange() {{
  var sel = document.getElementById('radarSelect');
  var opt = sel.options[sel.selectedIndex];
  var val = opt.value;
  if (val.startsWith('link_')) {{
    window.open(opt.getAttribute('data-url'), '_blank');
    return;
  }}
  document.querySelectorAll('#radarImages img').forEach(function(img) {{
    img.style.display = (img.id === val) ? 'block' : 'none';
  }});
}}
</script>
</body></html>"""

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

    print("Fetching lightning observations...")
    try:
        lightning_time, strikes = fetch_lightning()
        print(f"  updated: {lightning_time}, {len(strikes)} strikes in latest reading")
    except Exception as e:
        print(f"  lightning fetch failed (non-fatal): {e}")
        lightning_time, strikes = None, []

    print("Fetching radar snapshots...")
    radar_status = {}
    docs_dir = os.path.dirname(REPORT_PATH) or "."
    for key in RADAR_PATTERNS:
        out_path = os.path.join(docs_dir, f"radar_{key}.png")
        ok, frame_time = download_radar_image(key, out_path)
        radar_status[key] = (ok, frame_time)
        print(f"  {key}: {'ok, frame ' + frame_time if ok else 'unavailable this run'}")

    rain_forecast_count, wet_gauge_count = append_log(
        forecast_rows, rainfall_rows, forecast_time, rainfall_time
    )
    write_report(forecast_time, forecast_rows, rainfall_time, rainfall_rows,
                 radar_status, lightning_time, strikes)

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
