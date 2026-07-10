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
ICONS = [
    (["thunder"], "⛈️"),
    (["heavy rain", "moderate rain"], "🌧️"),
    (["rain", "shower", "drizzle"], "🌦️"),
    (["hazy", "mist", "fog"], "🌫️"),
    (["partly cloudy"], "⛅"),
    (["cloudy", "overcast"], "☁️"),
    (["fair", "sunny", "clear", "warm"], "☀️"),
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
PSI_LEVELS = [
    (50, "Good", "#37c9a1"),
    (100, "Moderate", "#8fb8d9"),
    (200, "Unhealthy", "#e8a24d"),
    (300, "Very Unhealthy", "#e8654d"),
    (float("inf"), "Hazardous", "#c084fc"),
]


def psi_category(value):
    if value is None:
        return "—", "#555555"
    for threshold, label, color in PSI_LEVELS:
        if value <= threshold:
            return label, color
    return "—", "#555555"


def get_psi():
    """PSI (24-hr Pollutant Standards Index) by region. Best-effort — schema
    not yet confirmed against a real response, so a mismatch here just
    results in an empty reading rather than breaking the report."""
    try:
        data = fetch("https://api-open.data.gov.sg/v2/real-time/api/psi")
        latest = data["items"][-1]
        readings = latest["readings"]["psi_twenty_four_hourly"]
        return latest.get("timestamp"), readings
    except Exception as e:
        print(f"  PSI fetch failed (non-fatal): {e}")
        return None, {}


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


def build_air_quality_html(psi_time, psi_readings):
    if not psi_readings:
        return ""
    order = ["national", "north", "south", "east", "west", "central"]
    cards = []
    for region in order:
        if region not in psi_readings:
            continue
        value = psi_readings[region]
        label, color = psi_category(value)
        cards.append(f"""
        <div class="stat">
          <b style="color:{color};">{value}</b>
          <span>{region.capitalize()} — {label}</span>
        </div>""")
    if not cards:
        return ""
    return f"""
  <div class="panel" style="margin-bottom:18px;">
    <h2 style="font-size:12px;text-transform:uppercase;color:#7f9aab;margin:0 0 10px;">Air quality (PSI, 24-hr) — updated {to_sgt(psi_time)}</h2>
    <div class="stats" style="margin-bottom:0;">{"".join(cards)}</div>
  </div>"""


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
    import json as _json

    towns = [
        {"lat": a["lat"], "lon": a["lon"], "icon": a["icon"], "name": a["name"], "text": a["text"]}
        for a in areas if a["lat"]
    ]
    wet_gauges = [
        {"lat": g["lat"], "lon": g["lon"], "mm": g["mm"], "name": g["name"]}
        for g in gauges if g["wet"] and g["lat"]
    ]
    towns_json = _json.dumps(towns)
    gauges_json = _json.dumps(wet_gauges)

    return f"""
    <div id="sg-map" style="height:440px;border-radius:8px;overflow:hidden;"></div>
    <script>
    (function() {{
      var map = L.map('sg-map', {{scrollWheelZoom: false}}).setView([1.352, 103.82], 11);
      L.tileLayer('https://{{s}}.basemaps.cartocdn.com/dark_all/{{z}}/{{x}}/{{y}}{{r}}.png', {{
        attribution: '&copy; OpenStreetMap contributors &copy; CARTO',
        maxZoom: 18
      }}).addTo(map);

      var towns = {towns_json};
      towns.forEach(function(t) {{
        var icon = L.divIcon({{
          html: '<div style="font-size:18px;text-align:center;line-height:1;">' + t.icon +
                '<div style="font-size:8px;color:#dce6ea;font-family:monospace;white-space:nowrap;">' + t.name + '</div></div>',
          className: '', iconSize: [60, 30], iconAnchor: [30, 15]
        }});
        L.marker([t.lat, t.lon], {{icon: icon}}).addTo(map).bindTooltip(t.name + ': ' + t.text);
      }});

      var gauges = {gauges_json};
      gauges.forEach(function(g) {{
        L.circle([g.lat, g.lon], {{
          radius: 700 + Math.min(g.mm, 20) * 100,
          color: '#e8654d', fill: false, weight: 2
        }}).addTo(map).bindTooltip(g.name + ': ' + g.mm.toFixed(1) + 'mm');
      }});
    }})();
    </script>"""


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


def build_report(forecast_time, areas, rainfall_time, gauges, lightning_count, outlook, avg_temp, avg_humidity, four_day, psi_time, psi_readings):
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
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
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
  {build_air_quality_html(psi_time, psi_readings)}
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
    psi_time, psi_readings = get_psi()

    log_run(areas, gauges, forecast_time, rainfall_time)
    build_report(forecast_time, areas, rainfall_time, gauges, lightning_count, outlook, avg_temp, avg_humidity, four_day, psi_time, psi_readings)

    print(f"Forecast: {len(areas)} towns, {sum(a['is_rain'] for a in areas)} showing rain")
    print(f"Gauges: {len(gauges)} stations, {sum(g['wet'] for g in gauges)} currently wet")
    print(f"Lightning strikes in last 30 min: {lightning_count}")
    print(f"Current weather: {outlook}, avg temp {avg_temp}, avg humidity {avg_humidity}")
    print(f"4-day outlook: {len(four_day)} days fetched")
    print(f"PSI: {psi_readings}")
    print(f"Report: {os.path.abspath(REPORT_PATH)}")


if __name__ == "__main__":
    main()
