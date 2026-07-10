"""
Rain Watch SG — NEA 2-hour forecast vs real rain-gauge readings, on a map.
Run with: pip install requests && python rain_watch.py
"""

import csv
import os
from datetime import datetime, timedelta, timezone

import requests

SGT = timezone(timedelta(hours=8))
LOG_PATH = "data/rain_watch_log.csv"
REPORT_PATH = "docs/index.html"
BOUNDS = {"lat_min": 1.15, "lat_max": 1.47, "lon_min": 103.59, "lon_max": 104.05}

ICONS = [
    (["thunder"], "⛈️"),
    (["heavy rain", "moderate rain"], "🌧️"),
    (["rain", "shower", "drizzle"], "🌦️"),
    (["hazy", "mist", "fog"], "🌫️"),
    (["partly cloudy"], "⛅"),
    (["cloudy", "overcast"], "☁️"),
    (["fair", "sunny", "clear", "warm"], "☀️"),
]

# Very rough silhouette of mainland Singapore's coastline (lat, lon), just
# enough points to give the map visual context — not survey-accurate.
SG_OUTLINE = [
    (1.327, 103.636), (1.300, 103.660), (1.276, 103.705), (1.265, 103.775),
    (1.263, 103.820), (1.265, 103.855), (1.290, 103.870), (1.300, 103.905),
    (1.345, 103.965), (1.345, 104.010), (1.405, 103.985), (1.417, 103.955),
    (1.445, 103.905), (1.462, 103.850), (1.462, 103.800), (1.445, 103.760),
    (1.445, 103.700), (1.420, 103.680), (1.380, 103.660), (1.327, 103.636),
]


def fetch(url):
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    return r.json()["data"]


def pick(d, *keys):
    """NEA has changed field casing between camelCase and snake_case before —
    try each spelling in turn."""
    for k in keys:
        if k in d:
            return d[k]
    raise KeyError(f"none of {keys} found in {list(d.keys())}")


def icon_for(text):
    t = (text or "").lower()
    for keywords, icon in ICONS:
        if any(k in t for k in keywords):
            return icon
    return "❔"


def to_sgt(iso_str):
    if not iso_str:
        return "—"
    dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=SGT)
    return dt.astimezone(SGT).strftime("%Y-%m-%d %H:%M") + " SGT (UTC+8)"


def project(lat, lon):
    x = (lon - BOUNDS["lon_min"]) / (BOUNDS["lon_max"] - BOUNDS["lon_min"]) * 820 + 20
    y = (1 - (lat - BOUNDS["lat_min"]) / (BOUNDS["lat_max"] - BOUNDS["lat_min"])) * 400 + 20
    return x, y


def get_forecast():
    data = fetch("https://api-open.data.gov.sg/v2/real-time/api/two-hr-forecast")
    latest = data["items"][-1]
    area_meta = pick(data, "areaMetadata", "area_metadata")
    locations = {a["name"]: pick(a, "labelLocation", "label_location") for a in area_meta}

    areas = []
    for f in latest["forecasts"]:
        loc = locations.get(f["area"], {})
        areas.append({
            "name": f["area"],
            "text": f["forecast"],
            "icon": icon_for(f["forecast"]),
            "is_rain": any(k in f["forecast"].lower() for k in ("rain", "shower", "thunder", "drizzle")),
            "lat": loc.get("latitude"),
            "lon": loc.get("longitude"),
        })
    updated = latest.get("update_timestamp") or latest.get("timestamp")
    return updated, areas


def get_rainfall():
    data = fetch("https://api-open.data.gov.sg/v2/real-time/api/rainfall")
    latest = data["readings"][-1]
    stations = {s.get("id", s.get("deviceId")): s for s in data["stations"]}

    gauges = []
    for reading in latest["data"]:
        sid = reading.get("stationId", reading.get("station_id"))
        s = stations.get(sid, {})
        loc = s.get("location", s.get("labelLocation", {}))
        gauges.append({
            "name": s.get("name", sid),
            "mm": reading["value"],
            "wet": reading["value"] > 0.2,
            "lat": loc.get("latitude"),
            "lon": loc.get("longitude"),
        })
    return latest.get("timestamp"), gauges


def get_station_readings(url):
    """Shared logic for air-temperature and relative-humidity, which use the
    same station/reading shape as rainfall."""
    data = fetch(url)
    latest = data["readings"][-1]
    values = [reading["value"] for reading in latest["data"]]
    return latest.get("timestamp"), values


def get_temperature():
    return get_station_readings("https://api-open.data.gov.sg/v2/real-time/api/air-temperature")


def get_humidity():
    return get_station_readings("https://api-open.data.gov.sg/v2/real-time/api/relative-humidity")


def get_lightning_count():
    """Returns how many strikes were recorded in the last 30 minutes.
    Best-effort — NEA's lightning feed schema is less predictable than the
    others, so any parsing issue here just results in 0 rather than a crash."""
    try:
        data = fetch("https://api-open.data.gov.sg/v2/real-time/api/lightning")
        latest = data["records"][-1]
        cutoff = datetime.now(SGT) - timedelta(minutes=30)
        count = 0
        for reading in latest.get("readings", []):
            for feature in reading.get("features", []):
                ts = feature.get("properties", {}).get("datetime")
                if ts:
                    t = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(SGT)
                    if t >= cutoff:
                        count += 1
        return count
    except Exception:
        return 0


RAIN_CHANCE = [
    (["thunder", "rain", "shower", "drizzle"], "High"),
    (["cloudy", "overcast", "hazy", "mist", "fog"], "Medium"),
    (["fair", "sunny", "clear", "warm", "partly cloudy"], "Low"),
]


def rain_chance_for(text):
    t = (text or "").lower()
    for keywords, level in RAIN_CHANCE:
        if any(k in t for k in keywords):
            return level
    return "—"


def get_four_day_outlook():
    """NEA's real outlook horizon tops out at 4 days — there's no genuine
    7-day Singapore forecast, so this is the furthest-out legitimate data
    available. Best-effort: any parsing issue just returns an empty list."""
    try:
        data = fetch("https://api-open.data.gov.sg/v2/real-time/api/four-day-outlook")
        latest = data["records"][-1]
        days = []
        for f in latest["forecasts"]:
            temp = f.get("temperature", {})
            humidity = f.get("relativeHumidity") or f.get("relative_humidity", {})
            days.append({
                "label": f.get("day") or f.get("date"),
                "text": f.get("forecast"),
                "icon": icon_for(f.get("forecast")),
                "chance": rain_chance_for(f.get("forecast")),
                "temp_low": temp.get("low"),
                "temp_high": temp.get("high"),
                "humidity_low": humidity.get("low"),
                "humidity_high": humidity.get("high"),
            })
        return days
    except Exception as e:
        print(f"  4-day outlook fetch failed (non-fatal): {e}")
        return []
def get_current_weather():
    """24-hour general outlook + live average temperature/humidity, for a
    'right now' summary alongside the 2-hour forecast map. Best-effort: if
    any piece fails, that piece is just skipped rather than breaking the report."""
    try:
        outlook_data = fetch("https://api-open.data.gov.sg/v2/real-time/api/twenty-four-hr-forecast")
        general = outlook_data["records"][-1]["general"]
        outlook = {
            "forecast": general["forecast"],
            "temp_low": general["temperature"]["low"],
            "temp_high": general["temperature"]["high"],
            "humidity_low": general["relative_humidity"]["low"],
            "humidity_high": general["relative_humidity"]["high"],
        }
    except Exception as e:
        print(f"  24hr outlook fetch failed (non-fatal): {e}")
        outlook = None

    try:
        _, temps = get_temperature()
        avg_temp = round(sum(temps) / len(temps), 1) if temps else None
    except Exception as e:
        print(f"  air temperature fetch failed (non-fatal): {e}")
        avg_temp = None

    try:
        _, humidities = get_humidity()
        avg_humidity = round(sum(humidities) / len(humidities)) if humidities else None
    except Exception as e:
        print(f"  humidity fetch failed (non-fatal): {e}")
        avg_humidity = None

    return outlook, avg_temp, avg_humidity


def build_outlook_strip(outlook, four_day):
    cards = []
    if outlook:
        cards.append({
            "label": "Today",
            "text": outlook["forecast"],
            "icon": icon_for(outlook["forecast"]),
            "chance": rain_chance_for(outlook["forecast"]),
            "temp_low": outlook["temp_low"],
            "temp_high": outlook["temp_high"],
            "humidity_low": outlook["humidity_low"],
            "humidity_high": outlook["humidity_high"],
        })
    cards.extend(four_day)
    if not cards:
        return ""

    day_html = []
    for c in cards:
        day_html.append(f"""
        <div style="flex:1;min-width:120px;background:#0c1c2c;border:1px solid #1c3a4f;border-radius:8px;padding:12px;text-align:center;">
          <div style="font-size:11px;color:#7f9aab;text-transform:uppercase;margin-bottom:6px;">{c['label']}</div>
          <div style="font-size:26px;">{c['icon']}</div>
          <div style="font-size:11px;color:#dce6ea;margin:4px 0;">{c['text']}</div>
          <div style="font-family:monospace;font-size:12px;color:#37c9a1;">{c['temp_low']}–{c['temp_high']}°C</div>
          <div style="font-family:monospace;font-size:11px;color:#7f9aab;">{c['humidity_low']}–{c['humidity_high']}% humidity</div>
          <div style="font-size:11px;color:#e8a24d;margin-top:4px;">Rain chance: {c['chance']}</div>
        </div>""")

    return f"""
  <div class="panel" style="margin-top:18px;">
    <h2 style="font-size:12px;text-transform:uppercase;color:#7f9aab;margin:0 0 10px;">4-day outlook (NEA doesn't forecast further than this for Singapore)</h2>
    <div style="display:flex;gap:10px;flex-wrap:wrap;">{"".join(day_html)}</div>
  </div>"""


def build_map(areas, gauges):
    outline_points = " ".join(f"{x:.0f},{y:.0f}" for x, y in (project(lat, lon) for lat, lon in SG_OUTLINE))
    parts = [
        '<svg viewBox="0 0 860 440" xmlns="http://www.w3.org/2000/svg" style="width:100%;height:auto;">',
        '<rect width="860" height="440" rx="10" fill="#0c1c2c"/>',
        f'<polygon points="{outline_points}" fill="#13293f" stroke="#2a5474" stroke-width="1.5"/>',
    ]

    for g in gauges:
        if g["wet"] and g["lat"]:
            x, y = project(g["lat"], g["lon"])
            r = 14 + min(g["mm"], 20)
            parts.append(f'<circle cx="{x:.0f}" cy="{y:.0f}" r="{r:.0f}" fill="none" '
                         f'stroke="#e8654d" stroke-width="2"><title>{g["name"]}: {g["mm"]:.1f}mm</title></circle>')

    for a in areas:
        if not a["lat"]:
            continue
        x, y = project(a["lat"], a["lon"])
        parts.append(f'<text x="{x:.0f}" y="{y:.0f}" font-size="20" text-anchor="middle" '
                     f'dominant-baseline="central">{a["icon"]}<title>{a["name"]}: {a["text"]}</title></text>')
        parts.append(f'<text x="{x:.0f}" y="{y+14:.0f}" font-size="8" text-anchor="middle" '
                     f'fill="#dce6ea" font-family="monospace">{a["name"]}</text>')

    parts.append("</svg>")
    return "".join(parts)


def log_run(areas, gauges, forecast_time, rainfall_time):
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    is_new = not os.path.exists(LOG_PATH)
    with open(LOG_PATH, "a", newline="") as f:
        w = csv.writer(f)
        if is_new:
            w.writerow(["logged_at", "forecast_updated", "rainfall_updated",
                        "areas_total", "areas_rain", "gauges_total", "gauges_wet"])
        w.writerow([datetime.now(SGT).isoformat(timespec="seconds"), forecast_time, rainfall_time,
                    len(areas), sum(a["is_rain"] for a in areas),
                    len(gauges), sum(g["wet"] for g in gauges)])


def build_report(forecast_time, areas, rainfall_time, gauges, lightning_count, outlook, avg_temp, avg_humidity, four_day):
    rain_count = sum(a["is_rain"] for a in areas)
    wet_count = sum(g["wet"] for g in gauges)
    alert = (f'<div class="alert">⚡ {lightning_count} lightning strike(s) in the last 30 minutes — '
             f'NEA advises suspending outdoor activities.</div>') if lightning_count else ""

    current_weather_html = ""
    if outlook or avg_temp is not None or avg_humidity is not None:
        bits = []
        if avg_temp is not None:
            bits.append(f'<div class="stat"><b>{avg_temp}°C</b><span>Current avg. temperature</span></div>')
        if avg_humidity is not None:
            bits.append(f'<div class="stat"><b>{avg_humidity}%</b><span>Current avg. humidity</span></div>')
        if outlook:
            bits.append(f'<div class="stat"><b>{outlook["temp_low"]}–{outlook["temp_high"]}°C</b><span>Today\'s range</span></div>')
        current_weather_html = f"""
  <div class="panel" style="margin-bottom:18px;">
    <h2 style="font-size:12px;text-transform:uppercase;color:#7f9aab;margin:0 0 10px;">Current weather{f' — {outlook["forecast"]}' if outlook else ''}</h2>
    <div class="stats" style="margin-bottom:0;">{''.join(bits)}</div>
  </div>"""

    html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><title>Rain Watch SG</title>
<style>
  body {{ background:#0a1420; color:#dce6ea; font-family:-apple-system,system-ui,sans-serif; padding:30px; }}
  .wrap {{ max-width:920px; margin:0 auto; }}
  .sub {{ color:#7f9aab; font-family:monospace; font-size:13px; margin-bottom:20px; }}
  .stats {{ display:flex; gap:14px; flex-wrap:wrap; margin-bottom:20px; }}
  .stat {{ background:#0f1e2e; border:1px solid #1c3a4f; border-radius:10px; padding:14px 18px; flex:1; min-width:160px; }}
  .stat b {{ font-family:monospace; font-size:24px; color:#37c9a1; display:block; }}
  .stat span {{ font-size:12px; color:#7f9aab; }}
  .panel {{ background:#0f1e2e; border:1px solid #1c3a4f; border-radius:10px; padding:16px 18px; }}
  .legend {{ display:flex; gap:16px; flex-wrap:wrap; font-size:12.5px; color:#7f9aab; margin-top:10px; }}
  .alert {{ background:#3a2410; border:1px solid #e8a24d; color:#f0c48a; border-radius:10px; padding:12px 16px; margin-bottom:18px; font-weight:600; }}
  footer {{ color:#7f9aab; font-size:12px; font-family:monospace; border-top:1px solid #1c3a4f; padding-top:14px; margin-top:16px; }}
</style></head>
<body><div class="wrap">
  <h1>Rain Watch SG</h1>
  <div class="sub">forecast updated {to_sgt(forecast_time)} · gauges updated {to_sgt(rainfall_time)}</div>
  {alert}
  {current_weather_html}
  <div class="stats">
    <div class="stat"><b>{len(areas)}</b><span>Towns in forecast</span></div>
    <div class="stat"><b>{rain_count}</b><span>Forecast as rain/showers</span></div>
    <div class="stat"><b>{wet_count}</b><span>Gauges actually recording rain</span></div>
  </div>
  <div class="panel">
    <h2 style="font-size:12px;text-transform:uppercase;color:#7f9aab;margin:0 0 10px;">Weather by town</h2>
    {build_map(areas, gauges)}
    <div class="legend">
      <span>☀️ sunny</span><span>⛅ partly cloudy</span><span>☁️ cloudy</span>
      <span>🌦️ light rain</span><span>🌧️ rain</span><span>⛈️ thundery</span><span>🌫️ hazy</span>
      <span style="color:#e8654d;">◯ = gauge recording rain now</span>
    </div>
  </div>
  {build_outlook_strip(outlook, four_day)}
  <footer>Data: data.gov.sg / NEA. All times shown in Singapore time (UTC+8). Log: {LOG_PATH}</footer>
</div></body></html>"""

    os.makedirs(os.path.dirname(REPORT_PATH), exist_ok=True)
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        f.write(html)


def main():
    forecast_time, areas = get_forecast()
    rainfall_time, gauges = get_rainfall()
    lightning_count = get_lightning_count()
    outlook, avg_temp, avg_humidity = get_current_weather()
    four_day = get_four_day_outlook()

    log_run(areas, gauges, forecast_time, rainfall_time)
    build_report(forecast_time, areas, rainfall_time, gauges, lightning_count, outlook, avg_temp, avg_humidity, four_day)

    print(f"Forecast: {len(areas)} towns, {sum(a['is_rain'] for a in areas)} showing rain")
    print(f"Gauges: {len(gauges)} stations, {sum(g['wet'] for g in gauges)} currently wet")
    print(f"Lightning strikes in last 30 min: {lightning_count}")
    print(f"Current weather: {outlook}, avg temp {avg_temp}, avg humidity {avg_humidity}")
    print(f"4-day outlook: {len(four_day)} days fetched")
    print(f"Report: {os.path.abspath(REPORT_PATH)}")


if __name__ == "__main__":
    main()
