"""Supply chain risk radar: public weather + news signals scored per supplier location."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote
from xml.etree import ElementTree as ET

import time

import httpx

LAST_ERR = ""
_WX, _GEO = {}, {}

TYPES = {"strike", "flood", "storm", "fire", "port", "sanctions", "unrest", "power", "heat", "other"}
ACTIONS = {
    "storm": "Pull forward open orders and add 1 to 2 weeks of buffer stock. Confirm the transport route.",
    "flood": "Check road and rail access. Ask for shipment status and consider an alternate route.",
    "strike": "Ask the supplier for shipment status today. Line up an alternate carrier or port.",
    "port": "Check delays at the port. Consider rerouting or earlier dispatch.",
    "fire": "Confirm production status and a recovery date. Qualify a backup supplier.",
    "sanctions": "Get a compliance review before placing new orders.",
    "unrest": "Confirm delivery dates and hold extra stock for critical items.",
    "power": "Ask whether production is affected and when it resumes.",
    "heat": "Confirm the production and loading schedule for the hot days.",
    "other": "Contact the supplier and confirm delivery dates.",
}


def band(score):
    return "Low" if score < 25 else "Medium" if score < 50 else "High" if score < 75 else "Severe"


def geocode(place):
    if place in _GEO:
        return _GEO[place]
    g = _geocode(place)
    if g:
        _GEO[place] = g
    return g


def _geocode(place):
    try:
        res = httpx.get("https://geocoding-api.open-meteo.com/v1/search",
                        params={"name": place.split(",")[0].strip(), "count": 5, "language": "en"}, timeout=10).json().get("results") or []
        hint = place.lower()
        for x in res:
            if (x.get("country") or "").lower() in hint or (x.get("admin1") or "").lower() in hint:
                return x
        return res[0] if res else None
    except Exception:
        return None


def _mx(v):
    return max([x for x in (v or []) if x is not None] or [0])


def _get_json(url, params=None, headers=None, tries=3):
    last = "no response"
    for n in range(tries):
        try:
            resp = httpx.get(url, params=params, headers=headers, timeout=12, follow_redirects=True)
            if resp.status_code == 200:
                return resp.json()
            last = f"HTTP {resp.status_code} {resp.text[:80]}"
            if resp.status_code not in (429, 500, 502, 503, 504):
                break
        except Exception as ex:
            last = repr(ex)[:100]
        time.sleep(0.8 * (n + 1))
    raise RuntimeError(last)


def _open_meteo(lat, lon):
    d = _get_json("https://api.open-meteo.com/v1/forecast", {
        "latitude": lat, "longitude": lon, "forecast_days": 7, "timezone": "auto",
        "daily": "precipitation_sum,wind_gusts_10m_max,temperature_2m_max"})["daily"]
    return {"rain_mm": _mx(d["precipitation_sum"]), "gust_kmh": _mx(d["wind_gusts_10m_max"]), "temp_c": _mx(d["temperature_2m_max"])}


def _met_no(lat, lon):
    """Backup source: MET Norway forecast. Gusts are estimated from sustained wind."""
    j = _get_json("https://api.met.no/weatherapi/locationforecast/2.0/compact", {"lat": round(lat, 4), "lon": round(lon, 4)},
                  {"User-Agent": "AthenaStudentProject/1.0 (college project)"})
    days, rain, wind, temp = [], {}, 0.0, -99.0
    for ts in j["properties"]["timeseries"]:
        day = ts["time"][:10]
        if day not in days:
            if len(days) >= 7:
                break
            days.append(day)
        det = ts["data"]["instant"]["details"]
        temp = max(temp, det.get("air_temperature", -99))
        wind = max(wind, det.get("wind_speed", 0))
        nxt = ts["data"].get("next_1_hours") or ts["data"].get("next_6_hours")
        if nxt:
            rain[day] = rain.get(day, 0) + (nxt["details"].get("precipitation_amount") or 0)
    return {"rain_mm": max(rain.values() or [0]), "gust_kmh": wind * 3.6 * 1.5, "temp_c": temp if temp > -99 else 0}


def weather(lat, lon):
    """7-day forecast summary from Open-Meteo, falling back to MET Norway. Returns a dict or None."""
    global LAST_ERR
    key = (round(lat, 2), round(lon, 2))
    hit = _WX.get(key)
    if hit and time.time() - hit[0] < 3 * 3600:
        return hit[1]
    errs = []
    for name, fn in (("Open-Meteo", _open_meteo), ("MET Norway", _met_no)):
        try:
            w = {**fn(lat, lon), "source": name}
            _WX[key] = (time.time(), w)
            return w
        except Exception as ex:
            errs.append(f"{name}: {str(ex)[:110]}")
    LAST_ERR = "; ".join(errs)
    return None


def weather_risk(w):
    pts, out = 0, []
    name = w.get("source", "Forecast")
    src = {"source": f"{name} 7-day forecast", "url": "https://open-meteo.com/" if name == "Open-Meteo" else "https://www.met.no/"}
    if w["rain_mm"] >= 100:
        pts += 30; out.append({"type": "flood", "text": f"Very heavy rain forecast, up to {w['rain_mm']:.0f} mm in a day", **src})
    elif w["rain_mm"] >= 50:
        pts += 18; out.append({"type": "flood", "text": f"Heavy rain forecast, up to {w['rain_mm']:.0f} mm in a day", **src})
    if w["gust_kmh"] >= 80:
        pts += 28; out.append({"type": "storm", "text": f"Damaging wind gusts forecast, up to {w['gust_kmh']:.0f} km/h", **src})
    elif w["gust_kmh"] >= 60:
        pts += 14; out.append({"type": "storm", "text": f"Strong wind gusts forecast, up to {w['gust_kmh']:.0f} km/h", **src})
    if w["temp_c"] >= 45:
        pts += 15; out.append({"type": "heat", "text": f"Extreme heat forecast, up to {w['temp_c']:.0f} °C", **src})
    elif w["temp_c"] >= 41:
        pts += 7; out.append({"type": "heat", "text": f"Very hot days forecast, up to {w['temp_c']:.0f} °C", **src})
    return pts, out


def news(place, n=6):
    q = f'{place} (strike OR flood OR cyclone OR fire OR protest OR shutdown OR sanctions OR "port congestion" OR blockade)'
    cutoff = datetime.now(timezone.utc) - timedelta(days=14)
    out = []
    try:
        r = httpx.get(f"https://news.google.com/rss/search?q={quote(q)}&hl=en-US&gl=US&ceid=US:en", timeout=10, follow_redirects=True)
        for it in ET.fromstring(r.content).iter("item"):
            try:
                if parsedate_to_datetime(it.findtext("pubDate", "")) < cutoff:
                    continue
            except Exception:
                continue
            out.append({"title": it.findtext("title", ""), "url": it.findtext("link", ""), "publisher": it.findtext("source") or ""})
            if len(out) >= n:
                break
    except Exception:
        pass
    return out


def build(suppliers, ask_json):
    items = suppliers[:10]

    def prep(s):
        g = geocode(s["location"])
        return {"geo": g, "weather": weather(g["latitude"], g["longitude"]) if g else None, "news": news(s["location"])}

    with ThreadPoolExecutor(3) as ex:
        pre = list(ex.map(prep, items))

    lines = []
    for i, (s, p) in enumerate(zip(items, pre)):
        if p["news"]:
            lines.append(f"{i + 1}) {s['name']} in {s['location']}")
            lines += [f"  {i + 1}.{j + 1}: {n['title']}" for j, n in enumerate(p["news"])]
    events = {}
    if lines:
        prompt = ("For each supplier, decide which headlines describe a real, current risk to deliveries from that location "
                  "(strike, flood, storm, fire, port congestion, sanctions, unrest, power cuts, other). Ignore unrelated, old or opinion items.\n"
                  + "\n".join(lines)
                  + '\n\nReturn ONLY JSON: {"events":[{"id":"1.1","type":"strike|flood|storm|fire|port|sanctions|unrest|power|other","severity":1,"why":"under 15 words"}]}\n'
                    "Severity 3 = deliveries stopped or very likely delayed, 2 = possible delays, 1 = minor. Include only relevant headlines.")
        try:
            events = {str(e.get("id")): e for e in ask_json(prompt).get("events", [])}
        except Exception:
            events = {}

    out = []
    for i, (s, p) in enumerate(zip(items, pre)):
        g = p["geo"]
        if not g:
            out.append({**s, "place": "", "score": None, "band": "Unknown", "drivers": [], "actions": [], "weather": None,
                        "checked": None, "note": "Location not found. Try City, Country."})
            continue
        drivers, wpts, npts = [], 0, 0
        if p["weather"]:
            wpts, drivers = weather_risk(p["weather"])
        flagged = 0
        for j, n in enumerate(p["news"]):
            e = events.get(f"{i + 1}.{j + 1}")
            if not e:
                continue
            t = e.get("type") if e.get("type") in TYPES else "other"
            try:
                sev = max(1, min(3, int(e.get("severity", 1))))
            except Exception:
                sev = 1
            flagged += 1
            npts += sev * 10
            drivers.append({"type": t, "text": (str(e.get("why", ""))[:120] or n["title"]), "headline": n["title"],
                            "url": n["url"], "source": n["publisher"] or "News"})
        parts = []
        for x in (g.get("name"), g.get("admin1"), g.get("country")):
            if x and x not in parts:
                parts.append(x)
        place = ", ".join(parts)
        checked = {"weather": bool(p["weather"]), "weather_source": (p["weather"] or {}).get("source", ""),
                   "headlines": len(p["news"]), "flagged": flagged}
        if not p["weather"] and not p["news"]:
            out.append({**s, "place": place, "score": None, "band": "Unknown", "drivers": [], "actions": [], "weather": None,
                        "checked": checked, "note": "Could not load weather or news for this location right now. Try again in a few minutes."})
            continue
        score = min(100, min(50, wpts) + min(60, npts))
        types = []
        for d in drivers:
            if d["type"] not in types:
                types.append(d["type"])
        note = ""
        if not p["weather"]:
            note = "Weather data was unavailable, so this score uses news only."
        elif not drivers:
            note = "No weather or news risks flagged for this location."
        out.append({**s, "place": place, "score": score, "band": band(score), "drivers": drivers,
                    "actions": [ACTIONS[t] for t in types[:2]], "weather": p["weather"], "checked": checked, "note": note})

    out.sort(key=lambda x: -1 if x["score"] is None else x["score"], reverse=True)
    scored = [x for x in out if x["score"] is not None]
    total = sum(x["spend"] for x in scored)
    exposed = sum(x["spend"] for x in scored if x["score"] >= 50)
    return {
        "suppliers": out,
        "summary": {"count": len(out), "high_count": sum(1 for x in scored if x["score"] >= 50), "total_spend": total,
                    "exposed_spend": exposed, "exposed_pct": (exposed / total * 100) if total else 0,
                    "top": scored[0]["name"] if scored and scored[0]["score"] >= 25 else "None flagged"},
        "generated": datetime.now(timezone.utc).isoformat(timespec="minutes"),
    }
