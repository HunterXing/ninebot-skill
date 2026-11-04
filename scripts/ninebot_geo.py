#!/usr/bin/env python3
"""坐标 → 中文地址。

九号的车辆接口只给经纬度，不给地址文本。这里用第三方地理 API 补上。

数据源按「精度」与「可用性」排优先级，逐个回退：

1. **高德逆地理编码**（需要 `AMAP_KEY`）—— 街道级中文地址，精度最好。
   高德用 GCJ-02 基准，与九号 APP 显示的位置一致。
   ⚠️ Key 的平台类型必须是「Web 服务」，不是「Web端 (JS API)」：这里调的是
   REST 接口，JS API 的 Key 会被拒（`INVALID_USER_SCODE` / `10001`），
   而类型在控制台创建后不能改。
2. **Nominatim**（OSM，免 key）—— 路名 / 社区级中文。OSM 在中国的数据
   偏稀薄，通常能到「XX路 XX社区 XX高新区 XX区」这一级。
   ⚠️ 国内直连基本不通，必须走代理（本机 mihomo 7890）。
3. ~~和风 GeoAPI~~ —— 已移除：它需要第二个 API key，只为区级兜底，
   当兜底保证至少有值。

坐标系：车机上报的通常是 WGS-84。GCJ-02 与 WGS-84 在国内相差约 500 米，
足以让「路名」不同，但不影响「区 / 社区」这一级。`NINEBOT_COORD_SYS` 可切换
（`wgs84` / `gcj02`），默认 `wgs84`。

隐私：地址是真实住址附近的信息，只在本机与老邢自己的推送里出现；
仓库里不写死任何坐标，坐标永远来自九号接口。
"""
from __future__ import annotations

import gzip
import json
import math
import os
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

USER_AGENT = "ninebot-skill/1.0 (ninebot-vehicle-integration)"
PROXY_URL = os.environ.get("NINEBOT_GEO_PROXY", "http://127.0.0.1:7890")
CACHE_FILE = os.path.expanduser(
    os.environ.get("NINEBOT_GEO_CACHE", "~/.hermes/cache/ninebot_geo.json"))
CACHE_TTL = int(os.environ.get("NINEBOT_GEO_TTL", "21600"))  # 6 小时
TIMEOUT = int(os.environ.get("NINEBOT_GEO_TIMEOUT", "12"))

# 合肥一带，用于坐标偏移判定
_CHINA_BOUNDS = (72.004, 137.8347, 0.8293, 55.8271)


# --------------------------------------------------------------------------
# 坐标转换（GCJ-02 <-> WGS-84）
# --------------------------------------------------------------------------
def _out_of_china(lng: float, lat: float) -> bool:
    w, e, s, n = _CHINA_BOUNDS
    return not (w < lng < e and s < lat < n)


def _transform_lat(x: float, y: float) -> float:
    ret = (-100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y
           + 0.2 * math_sqrt(abs(x)))
    ret += (20.0 * math_sin(6.0 * x * math_pi) + 20.0 * math_sin(2.0 * x * math_pi)) * 2.0 / 3.0
    ret += (20.0 * math_sin(y * math_pi) + 40.0 * math_sin(y / 3.0 * math_pi)) * 2.0 / 3.0
    ret += (150.0 * math_sin(y / 12.0 * math_pi) + 300.0 * math_sin(y / 30.0 * math_pi)) * 2.0 / 3.0
    return ret


def _transform_lng(x: float, y: float) -> float:
    ret = (300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * math_sqrt(abs(x)))
    ret += (20.0 * math_sin(6.0 * x * math_pi) + 20.0 * math_sin(2.0 * x * math_pi)) * 2.0 / 3.0
    ret += (20.0 * math_sin(x * math_pi) + 40.0 * math_sin(x / 3.0 * math_pi)) * 2.0 / 3.0
    ret += (150.0 * math_sin(x / 12.0 * math_pi) + 300.0 * math_sin(x / 30.0 * math_pi)) * 2.0 / 3.0
    return ret


math_sqrt = math.sqrt
math_sin = math.sin
math_cos = math.cos
math_pi = math.pi


def wgs84_to_gcj02(lat: float, lon: float) -> Tuple[float, float]:
    if _out_of_china(lon, lat):
        return lat, lon
    dlat = _transform_lat(lon - 105.0, lat - 35.0)
    dlon = _transform_lng(lon - 105.0, lat - 35.0)
    radlat = lat / 180.0 * math_pi
    magic = math_sin(radlat)
    magic = 1 - magic * magic
    sq = math_sqrt(magic)
    dlat = (dlat * 180.0) / ((6335552.717 * (1 - (lat / 180.0 * math_pi) ** 2)) / (magic * sq) * math_pi)
    dlon = (dlon * 180.0) / ((6335552.717 * sq * math_cos(radlat)) * math_pi)
    return lat + dlat, lon + dlon


def gcj02_to_wgs84(lat: float, lon: float) -> Tuple[float, float]:
    if _out_of_china(lon, lat):
        return lat, lon
    glat, glon = wgs84_to_gcj02(lat, lon)
    return lat * 2 - glat, lon * 2 - glon


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
def _read_setting(key: str) -> str:
    """Read a setting from the environment or the local *.env files.

    Covers both credentials (AMAP_KEY) and plain settings
    (NINEBOT_COORD_SYS), so the whole geo configuration can live in one
    file the user fills in once.
    """
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


def _http(url: str, use_proxy: bool, headers: Optional[Dict[str, str]] = None
          ) -> Optional[str]:
    hdr = {"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"}
    if headers:
        hdr.update(headers)
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": PROXY_URL, "https": PROXY_URL}
                                    if use_proxy else {}))
    req = urllib.request.Request(url, headers=hdr)
    try:
        with opener.open(req, timeout=TIMEOUT) as resp:
            raw = resp.read()
            if resp.headers.get("Content-Encoding") == "gzip":
                try:
                    raw = gzip.decompress(raw)
                except OSError:
                    pass
            return raw.decode("utf-8", "replace")
    except (urllib.error.URLError, OSError, ValueError):
        return None


# --------------------------------------------------------------------------
# 各数据源
# --------------------------------------------------------------------------
# Amap error codes → what to actually do about them.
AMAP_ERRORS = {
    "10001": "Key 不正确或未激活",
    "10002": "安全密钥 jscode 不正确",
    "10003": "该 Key 开启了安全密钥校验，但请求没带 jscode",
    "10004": "该 Key 没有这个服务的调用权限",
    "10005": "Key 的服务平台类型与请求不符",
    "10009": "这个 Key 不是「Web服务」平台的 —— 控制台创建 Key 时服务平台选错了"
             "（Android / iOS / 微信小程序 / JS API 的 Key 都带「安全密钥」，"
             "很容易拿错；要 Web 服务就另建一个，平台选「Web服务」）",
    "20001": "配额用尽或未开通该服务",
    "30001": "坐标格式或取值错误",
    "40001": "签名校验失败（该 Key 用的是数字签名模式，不是 jscode）",
}


def _amap_error(raw: Optional[Dict[str, Any]]) -> str:
    """Turn an Amap rejection into a line the user can act on."""
    if not raw:
        return ("没有收到高德响应 —— 网络不通或端点不可达。"
                "注意 restapi.amap.com 直连可用，restapi.ammoo.com 在部分网络下"
                "解析到境外节点而不通")
    code = str(raw.get("infocode") or "")
    info = str(raw.get("info") or "")
    hint = AMAP_ERRORS.get(code)
    base = f"高德拒绝：info={info or '未知'} code={code or '未知'}"
    return f"{base} —— {hint}" if hint else base


def _amap_query(lat: float, lon: float) -> Optional[Dict[str, Any]]:
    """One raw Amap reverse-geocode call.

    Returns the parsed envelope (status/info/regeocode) or None when the
    request never completed. The caller distinguishes "key rejected"
    (status 0 with an info code) from "network blocked" (None).
    """
    key = _read_setting("AMAP_KEY")
    if not key:
        return None
    # restapi.amap.com (v3) is the endpoint that resolves to a mainland
    # node from here; restapi.ammoo.com (v3.2) resolves to an overseas
    # address and times out. Override with AMAP_BASE_URL if needed.
    base = _read_setting("AMAP_BASE_URL") or "https://restapi.amap.com"
    url = (f"{base.rstrip('/')}/v3/geocode/regeo"
           f"?key={key}&location={lon},{lat}&extensions=all")
    # New Amap keys are issued together with a security code; the request
    # has to carry it as `jscode` or Amap rejects it with code 10003.
    jscode = _read_setting("AMAP_JSCODE")
    if jscode:
        url += f"&jscode={jscode}"
    body = _http(url, use_proxy=False)
    if not body:
        body = _http(url, use_proxy=True)
    if not body:
        return None
    try:
        return json.loads(body)
    except ValueError:
        return None


def _from_amap(lat: float, lon: float) -> Optional[Dict[str, Any]]:
    """高德逆地理编码 —— 街道级中文，GCJ-02 基准。

    `extensions=all` returns the nearby POI list; each entry carries its
    own `distance` in metres, which is what tells us whether the incoming
    coordinate was already GCJ-02 or still raw WGS-84.
    """
    parsed = _amap_query(lat, lon)
    if not parsed or str(parsed.get("status")) != "1":
        return None
    r = (parsed.get("regeocode") or {})
    comp = r.get("addressComponent") or {}
    pois = r.get("pois") or []
    poi_name = ""
    poi_distance: Optional[int] = None
    for poi in pois:
        if not isinstance(poi, dict):
            continue
        if not poi_name and poi.get("name"):
            poi_name = str(poi["name"])
        # Amap returns distance as a decimal STRING ("299.014"), so int()
        # on it raises and would silently drop the reading.
        raw_dist = poi.get("distance")
        if raw_dist is None:
            continue
        try:
            dist = int(float(raw_dist))
        except (TypeError, ValueError):
            continue
        if poi_distance is None or dist < poi_distance:
            poi_distance = dist
    # v3 puts the street in `streetNumber` (an object with the house
    # number); v3.2 puts it in `street` as an object with `name`.
    # Accept either, and fall back to the tail of formatted_address.
    street = ""
    number = ""
    sn = comp.get("streetNumber")
    if isinstance(sn, dict):
        street = sn.get("street") or ""
        number = sn.get("number") or ""
    elif isinstance(sn, str):
        number = sn
    st = comp.get("street")
    if isinstance(st, dict):
        street = street or st.get("name") or ""
        number = number or st.get("number") or ""
    elif isinstance(st, str):
        street = street or st

    township = comp.get("township") or ""
    district = comp.get("district") or ""
    city = comp.get("city") or ""
    if isinstance(city, list):
        city = city[0] if city else ""
    province = comp.get("province") or ""
    formatted = r.get("formatted_address") or ""
    if not street and formatted:
        # formatted_address looks like "省 市 区 街道 路"; the last
        # segment is the road it snapped to.
        segments = [x.strip() for x in formatted.split(" ") if x.strip()]
        if segments:
            street = segments[-1]

    parts = [x for x in (province, city, district, township) if x]
    if street:
        parts.append(f"{street}{number}")
    if poi_name:
        parts.append(poi_name)
    if not parts:
        return None

    # Small-to-large, Chinese reading order: POI → road → area → district.
    # The POI (a housing estate, a shop, a park) is the most precise thing
    # Amap gives us, so it leads.
    short_bits = [x for x in (poi_name, street, township, district) if x]
    short = " · ".join(short_bits) if short_bits else " ".join(parts)
    if city and city not in short:
        short = f"{short}（{city}）"
    return {
        "address": " ".join(parts),
        "short": short,
        "source": "amap",
        # The weather endpoint keys off adcode, so carry it through and
        # skip a separate "which city is this" lookup.
        "adcode": comp.get("adcode") or "",
        "province": province, "city": city, "district": district,
        "township": township, "street": street, "houseNumber": number,
        "poi": poi_name,
        "poiDistance": poi_distance,
        "formattedAddress": formatted,
    }


def _from_nominatim(lat: float, lon: float) -> Optional[Dict[str, Any]]:
    """Nominatim（OSM）—— 免 key，路名 / 社区级中文。国内必须走代理。"""
    url = ("https://nominatim.openstreetmap.org/reverse?format=jsonv2&zoom=18"
           f"&lat={lat:.6f}&lon={lon:.6f}&accept-language=zh-CN&addressdetails=1")
    body = _http(url, use_proxy=True)
    if not body:
        return None
    try:
        d = json.loads(body)
    except ValueError:
        return None
    if d.get("error"):
        return None
    a = d.get("address") or {}
    display = d.get("display_name") or ""
    road = a.get("road") or a.get("pedestrian") or a.get("footway") or ""
    neigh = a.get("neighbourhood") or a.get("quarter") or ""
    suburb = a.get("suburb") or a.get("city_district") or ""
    state = a.get("state") or ""
    country = a.get("country") or ""

    # OSM tags a Chinese district as `city` (e.g. 「XX区」), while
    # `display_name` carries the real city name (「XX市」) that the tag set
    # omits. Pull the
    # "…市" segment out of the display name to fill that gap.
    segments = [x.strip() for x in display.split(",") if x.strip()]
    city = ""
    for seg in reversed(segments):
        if seg.endswith("市") and seg != state:
            city = seg
            break
    if not city:
        city = a.get("town") or a.get("county") or ""
    district = a.get("city_district") or a.get("district") or a.get("county") \
        or a.get("city") or ""

    parts = [x for x in (country, state, city, district, suburb, neigh) if x]
    if road:
        parts.append(road)
    if not parts:
        return None
    # Chinese addresses read small-to-large: 路 → 社区 → 区 → 市
    short_bits = [x for x in (road, neigh, district) if x]
    short = " · ".join(short_bits) if short_bits else " ".join(parts)
    if city and city not in short:
        short = f"{short}（{city}）"
    return {
        "address": " ".join(parts),
        "short": short,
        "source": "nominatim",
        "province": state, "city": city, "district": district,
        "township": neigh or suburb, "street": road,
    }



def coord_system() -> str:
    """Which datum the incoming coordinates are in: "wgs84" or "gcj02"."""
    val = (_read_setting("NINEBOT_COORD_SYS") or "wgs84").strip().lower()
    return "gcj02" if val == "gcj02" else "wgs84"


def system_is_wgs() -> bool:
    """True when the incoming coordinates are treated as WGS-84."""
    return coord_system() != "gcj02"


def _cache_key(lat: float, lon: float, system: str) -> str:
    return f"{system}:{lat:.5f},{lon:.5f}"


def _cache_load() -> Dict[str, Any]:
    try:
        with open(CACHE_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _cache_store(key: str, value: Dict[str, Any]) -> None:
    data = _cache_load()
    data[key] = {"at": int(time.time()), "value": value}
    try:
        os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
        tmp = CACHE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, CACHE_FILE)
    except OSError:
        pass


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------
# Source priority — higher wins. A cached answer is only reused when no
# better source is currently usable.
_SOURCE_RANK = {"amap": 2, "nominatim": 1}


def _source_rank(source: Optional[str]) -> int:
    return _SOURCE_RANK.get((source or "").lower(), 0)


def _best_available_rank() -> int:
    """The highest-priority source that is configured right now."""
    if _read_setting("AMAP_KEY"):
        return _SOURCE_RANK["amap"]
    return _SOURCE_RANK["nominatim"]


def reverse_geocode(lat: Optional[float], lon: Optional[float],
                    use_cache: bool = True) -> Optional[Dict[str, Any]]:
    """经纬度 → 中文地址。多源回退；全失败返回 None。"""
    if lat is None or lon is None:
        return None
    try:
        lat = float(lat)
        lon = float(lon)
    except (TypeError, ValueError):
        return None
    if abs(lat) > 90 or abs(lon) > 180:
        return None

    system = coord_system()
    # Each geocoder has its own datum:
    #   Amap interprets `location` as GCJ-02.
    #   Nominatim / OSM is WGS-84.
    #   The vehicle reports one of the two, so translate per source —
    #   feeding Amap a WGS-84 pair shifts the answer by ~500 m.
    if system == "gcj02":
        wlat, wlon = gcj02_to_wgs84(lat, lon)   # for Nominatim (WGS-84)
        glat, glon = lat, lon                    # for Amap     (GCJ-02)
    else:
        wlat, wlon = lat, lon                    # for Nominatim (WGS-84)
        glat, glon = wgs84_to_gcj02(lat, lon)    # for Amap     (GCJ-02)

    qlat, qlon = wlat, wlon

    ckey = _cache_key(lat, lon, system)
    if use_cache:
        hit = _cache_load().get(ckey)
        if hit and isinstance(hit.get("value"), dict):
            if int(time.time()) - int(hit.get("at", 0)) < CACHE_TTL:
                out = dict(hit["value"])
                # A cached answer from a fallback source must not shadow a
                # better source that became available later — configuring
                # an Amap key after Nominatim answers were cached would
                # otherwise keep serving the coarse result for hours.
                if _source_rank(out.get("source")) >= _best_available_rank():
                    out["cached"] = True
                    return out

    # (source, latitude, longitude) — Amap gets the GCJ-02 pair.
    # QWeather used to sit here as a third fallback, but it required a
    # second API key for a district-level answer; the whole skill now
    # runs off the Amap key alone, so it is gone rather than optional.
    sources = (
        (_from_amap, glat, glon),
        (_from_nominatim, wlat, wlon),
    )
    for source, slat, slon in sources:
        try:
            got = source(slat, slon)
        except Exception:  # noqa: BLE001 - a dead source must not break the rest
            got = None
        if got:
            got["cached"] = False
            got["coords"] = {"lat": lat, "lon": lon, "system": system}
            if (os.environ.get("NINEBOT_GEO_VERIFY") or "").strip() in ("1", "true", "yes"):
                # Diagnostic only: it costs two extra geocoder calls.
                got["precision"] = _precision_hint(lat, lon)
            if use_cache:
                _cache_store(ckey, got)
            return got
        time.sleep(0.4)
    return None


def _precision_hint(lat: float, lon: float) -> str:
    """Say how much the street-level answer can be trusted.

    Only run when NINEBOT_GEO_VERIFY=1 — it doubles the request count and
    Nominatim asks for at most one request per second.

    A GPS fix parks somewhere near the vehicle, not on it — the usual
    parking offset is tens of metres, but the fix itself can be worse.
    The geocoder returns the nearest road, and 500 m of GCJ-02/WGS-84
    offset can land on a different road entirely. Cross-check both
    coordinate interpretations: when they agree on the neighbourhood but
    disagree on the road, the road name is a guess.
    """
    try:
        alt_lat, alt_lon = gcj02_to_wgs84(lat, lon) if system_is_wgs() \
            else wgs84_to_gcj02(lat, lon)
    except Exception:  # noqa: BLE001
        return "unknown"
    try:
        a = _from_nominatim(lat, lon)
        b = _from_nominatim(alt_lat, alt_lon)
    except Exception:  # noqa: BLE001
        return "unknown"
    if not a or not b:
        return "unknown"
    same_area = (a.get("district") == b.get("district")
                 and (a.get("township") or "") == (b.get("township") or ""))
    if same_area and a.get("street") != b.get("street"):
        return "street-approx"
    if same_area:
        return "street"
    return "area-only"


def amap_candidates(lat: float, lon: float) -> Dict[str, Any]:
    """Ask Amap about the same point under both datum readings.

    Amap interprets `location` as GCJ-02. The vehicle may report either
    GCJ-02 (already converted server-side) or raw GPS WGS-84. Guessing
    wrong shifts the answer ~500 m, which can name a different road.

    The disambiguator is Amap's own `distance` to the nearest POI: read
    correctly the point sits right on the vehicle, so the nearest POI is
    a few dozen metres away; read wrongly it is ~500 m off, so the
    nearest POI is further away.

    Returns both candidates plus which one Amap scored better.
    """
    as_gcj = _from_amap(lat, lon)                       # read as GCJ-02
    # If the value is WGS-84, convert it to GCJ-02 before asking Amap.
    glat, glon = wgs84_to_gcj02(lat, lon)
    as_wgs = _from_amap(glat, glon)                    # read as WGS-84

    def score(cand: Optional[Dict[str, Any]]) -> Optional[int]:
        if not cand:
            return None
        d = cand.get("poiDistance")
        return int(d) if d is not None else None

    d_gcj, d_wgs = score(as_gcj), score(as_wgs)
    winner = None
    if d_gcj is not None and d_wgs is not None:
        winner = "gcj02" if d_gcj < d_wgs else ("wgs84" if d_wgs < d_gcj else "tie")

    # Surface Amap's own rejection reason so the caller can tell a bad key
    # from a blocked network.
    raw = _amap_query(lat, lon)
    error = ""
    if not raw or str(raw.get("status")) != "1":
        error = _amap_error(raw)
    return {
        "as_gcj02": as_gcj,
        "as_wgs84": as_wgs,
        "distance_as_gcj02": d_gcj,
        "distance_as_wgs84": d_wgs,
        "winner": winner,
        "error": error,
    }


def format_address(geo: Optional[Dict[str, Any]]) -> str:
    """给播报用的一行中文地址。"""
    if not geo:
        return ""
    short = str(geo.get("short") or "").strip()
    if short:
        return short
    return str(geo.get("address") or "").strip()


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="坐标反查中文地址")
    ap.add_argument("lat", type=float, nargs="?")
    ap.add_argument("lon", type=float, nargs="?")
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--probe-amap", action="store_true",
                    help="Amap only: report both datum readings and their "
                         "nearest-POI distances, so the caller can tell which "
                         "datum the vehicle reports.")
    args = ap.parse_args()

    if args.probe_amap:
        if args.lat is None or args.lon is None:
            print(json.dumps({"error": "--probe-amap needs lat lon"},
                             ensure_ascii=False))
            raise SystemExit(2)
        if not _read_setting("AMAP_KEY"):
            print(json.dumps({"error": "AMAP_KEY not set"},
                             ensure_ascii=False))
            raise SystemExit(2)
        result = amap_candidates(args.lat, args.lon)
        if not result.get("as_gcj02") and not result.get("as_wgs84"):
            print(json.dumps({"error": result.get("error")
                                        or "amap returned nothing"},
                             ensure_ascii=False))
            raise SystemExit(1)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        raise SystemExit(0)

    if args.lat is None or args.lon is None:
        ap.error("lat and lon are required")
    res = reverse_geocode(args.lat, args.lon, use_cache=not args.no_cache)
    print(json.dumps(res, ensure_ascii=False, indent=2))
