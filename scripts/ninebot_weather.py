#!/usr/bin/env python3
"""Weather for the vehicle's location, via Amap.

Amap needs no second credential: the same Web-service key that reverse
geocodes the position also serves the weather, keyed by the `adcode` the
geocode call already returned. That keeps the whole skill on one key
instead of asking for a second provider's registration.

Field trade-off versus a dedicated weather API: Amap gives live
conditions plus a four-day forecast (day/night text, temperatures, wind),
but no sunrise/sunset, no UV index and no precipitation amount. Rain is
detected from the forecast text instead, which is what the report needs.

Credentials come from the environment or ~/.hermes/secrets/map.env —
never from a file inside the repository.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

GEO_API = "https://restapi.amap.com/v3/geocode/regeo"
WEATHER_API = "https://restapi.amap.com/v3/weather/weatherInfo"

USER_AGENT = "ninebot-skill/1.0 (ninebot-vehicle-integration)"
TIMEOUT = int(os.environ.get("NINEBOT_WEATHER_TIMEOUT", "12"))

WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")

# Amap reports `week` as 1..7 with 1 = Monday.
_AMAP_WEEK = {"1": "周一", "2": "周二", "3": "周三", "4": "周四",
              "5": "周五", "6": "周六", "7": "周日"}


def _read_setting(key: str) -> str:
    val = os.environ.get(key, "")
    if val:
        return val.strip()
    for path in (os.path.expanduser("~/.hermes/secrets/map.env"),
                 os.path.expanduser("~/.hermes/.env")):
        if not os.path.exists(path):
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    name, val2 = line.split("=", 1)
                    if name.strip() == key:
                        return val2.strip().strip('"').strip("'")
        except OSError:
            continue
    return ""


def _get(url: str) -> Optional[Dict[str, Any]]:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with opener.open(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, ValueError):
        return None


def adcode_for(lat: Optional[float], lon: Optional[float]) -> Optional[Dict[str, Any]]:
    """Coordinates → Amap adcode, for callers that have no address yet.

    The report normally passes the adcode its address lookup already
    produced, so this only runs when coordinates are all we have.
    """
    key = _read_setting("AMAP_KEY")
    if not key or lat is None or lon is None:
        return None
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        return None
    # Amap reads the position as GCJ-02; convert when the vehicle reports
    # WGS-84 so the district lands on the right side of a boundary.
    try:
        import ninebot_geo as geo
        if geo.coord_system() != "gcj02":
            lat, lon = geo.wgs84_to_gcj02(lat, lon)
    except Exception:  # noqa: BLE001 - best effort only
        pass
    d = _get(f"{GEO_API}?key={key}&location={lon},{lat}&extensions=base")
    if not d or str(d.get("status")) != "1":
        return None
    comp = ((d.get("regeocode") or {}).get("addressComponent") or {})
    adcode = comp.get("adcode")
    if not adcode:
        return None
    name = comp.get("district") or comp.get("city") or ""
    if isinstance(name, list):
        name = name[0] if name else ""
    return {"id": str(adcode), "name": name}


def _wind(text: Any) -> str:
    wind = str(text or "").strip()
    if wind and "风" not in wind:
        wind += "风"
    return wind


def current(adcode: str) -> Dict[str, Any]:
    """Live conditions right now."""
    key = _read_setting("AMAP_KEY")
    if not key or not adcode:
        return {}
    d = _get(f"{WEATHER_API}?key={key}&city={adcode}&extensions=base")
    lives = (d or {}).get("lives") or []
    if not lives:
        return {}
    live = lives[0]
    return {
        "text": live.get("weather") or "",
        "temp": live.get("temperature"),
        "humidity": live.get("humidity"),
        "windDir": _wind(live.get("winddirection")),
        "windScale": live.get("windpower") or "",
        "reportTime": live.get("reporttime") or "",
    }


def forecast(adcode: str) -> List[Dict[str, Any]]:
    """Four-day forecast, normalised to the fields the report prints."""
    key = _read_setting("AMAP_KEY")
    if not key or not adcode:
        return []
    d = _get(f"{WEATHER_API}?key={key}&city={adcode}&extensions=all")
    forecasts = (d or {}).get("forecasts") or []
    if not forecasts:
        return []
    out: List[Dict[str, Any]] = []
    for cast in forecasts[0].get("casts") or []:
        out.append({
            "date": cast.get("date") or "",
            "weekday": _AMAP_WEEK.get(str(cast.get("week") or ""), ""),
            "textDay": cast.get("dayweather") or "",
            "textNight": cast.get("nightweather") or "",
            "tempMin": cast.get("nighttemp"),
            "tempMax": cast.get("daytemp"),
            "windDirDay": _wind(cast.get("daywind")),
            "windScaleDay": cast.get("daypower") or "",
            # Amap carries no precipitation amount; the text is the signal.
            "precip": "",
        })
    return out


def weekday_of(date_str: str) -> str:
    """'2026-11-04' → '周三'."""
    try:
        y, m, d = (int(x) for x in date_str.split("-"))
    except (ValueError, AttributeError):
        return ""
    import datetime
    try:
        return WEEKDAYS[datetime.date(y, m, d).weekday()]
    except ValueError:
        return ""


def collect(lat: Optional[float], lon: Optional[float],
            adcode: Optional[str] = None,
            city_name: str = "") -> Dict[str, Any]:
    """Everything the report needs about the weather, in one call.

    `adcode` normally arrives from the address lookup that already ran,
    which saves a round-trip just to learn which city we are in.
    """
    if not _read_setting("AMAP_KEY"):
        return {}
    if not adcode:
        found = adcode_for(lat, lon)
        if not found:
            return {}
        adcode = found["id"]
        city_name = city_name or found.get("name", "")

    days = forecast(str(adcode))
    now = current(str(adcode))
    if not days and not now:
        return {}
    return {
        "city": {"id": str(adcode), "name": city_name},
        "days": days,
        "now": now,
    }


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="车辆位置天气（高德）")
    ap.add_argument("--adcode", help="Amap adcode, e.g. 340104")
    ap.add_argument("lat", type=float, nargs="?")
    ap.add_argument("lon", type=float, nargs="?")
    args = ap.parse_args()
    print(json.dumps(collect(args.lat, args.lon, args.adcode), ensure_ascii=False, indent=2))
