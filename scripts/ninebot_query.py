#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Ninebot vehicle query — cross-agent skill backend.

Zero third-party dependencies (stdlib only: urllib / json / argparse / os).

Two authentication backends:

  official  九号 Device Service  — Authorization: Bearer <NINEBOT_DEVICESERVICE_KEY>
             endpoints: cn-cbu-gateway.ninebot.com/ai-skill/api/device/info/*
             requires: an OpenClaw 内测 key (九号出行 APP「圈子」)

  account   九号账号直登        — 自己的九号账号密码 / 短信验证码
             passport login: api-passport-bj.ninebot.com
             vehicles:      ebike/steeldust business APIs (NET encrypted)
             requires: nothing but your own account

The backend is auto-detected: an explicit --backend wins, then
NINEBOT_DEVICESERVICE_KEY in the environment, then a saved account session,
then the account backend as the final fallback.

Security notes (these fix findings in the upstream official skill):
  * the API key is read from the environment only — never accepted as a
    command-line argument, so it cannot leak into shell history or `ps`.
  * a config file is read only from an explicit --config path; the legacy
    "pick up ./config.json from the current directory" behaviour is gone,
    so a stray file in an untrusted working directory cannot redirect
    authenticated requests (and therefore cannot leak the key).
  * the multi-device listing is projected down to sn + deviceName only,
    instead of dumping the whole upstream device object.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ninebot_client as client
import ninebot_geo as geo

# --------------------------------------------------------------------------
# Endpoints
# --------------------------------------------------------------------------

GATEWAY_BASE_URL = "https://cn-cbu-gateway.ninebot.com"
PASSPORT_BASE_URL = "https://api-passport-bj.ninebot.com"
EBIKE_BASE_URL = "https://ebike.ninebot.com"
MOTOR_BASE_URL = "https://steeldust.ninebot.com"

# The official Device Service, as published in the upstream skill.
OFFICIAL_CONFIG: Dict[str, Any] = {
    "backend": "official",
    "base_url": GATEWAY_BASE_URL,
    "auth": {"api_key_header": "Authorization", "api_key_prefix": "Bearer "},
    "devices": {
        "method": "GET",
        "path": "/ai-skill/api/device/info/get-device-list",
        "payload": {},
        "list_path": "data",
        "sn_field": "sn",
        "name_field": "deviceName",
    },
    "device_info": {
        "method": "POST",
        "path": "/ai-skill/api/device/info/get-device-dynamic-info",
        "payload": {"sn": "{sn}"},
        "battery_path": "data.dumpEnergy",
        "status_path": "data.powerStatus",
        "location_path": "data.locationInfo.locationDesc",
        "extra_fields": {
            "estimateMileage": "data.estimateMileage",
            "chargingState": "data.chargingState",
            "remainChargeTime": "data.remainChargeTime",
        },
    },
}

# The account backend no longer needs a route table: it speaks the encrypted
# business channel through `ninebot_client`, which owns the URLs and the
# NET envelope. Kept as a placeholder so the config merge keeps working and
# so `--config` overrides can still be applied per backend.
#
# NOTE: the old table pointed at `/app-api/inner/device/ai/*`, the route
# family Ninebot retired in March 2026 (live check: HTTP 404 with
# `requestId: oms-device-api-route-ai:...`). Do not restore it.
ACCOUNT_CONFIG: Dict[str, Any] = {
    "backend": "account",
    "base_url": EBIKE_BASE_URL,
    "auth": {},
    "devices": {
        "method": "GET",
        "path": "/vehicle/binding/my-vehicle",
        "payload": {},
        "list_path": "data",
        "sn_field": "sn",
        "name_field": "name",
    },
    "device_info": {
        "method": "POST",
        "path": "/vehicle/vehicle/desktop-component",
        "payload": {"sn": "{sn}"},
        "battery_path": "data.battery",
        "status_path": "data.powerStatus",
        "location_path": "data.location",
        "extra_fields": {},
    },
}

DEFAULT_TIMEOUT = 15
RETRY_ATTEMPTS = 3
RETRY_BACKOFF = 2.0

# The session file for the account backend.
#
# Default to the same secrets convention the rest of this machine uses
# (~/.hermes/secrets/*.env, 0600) rather than a per-tool config directory.
# Override with NINEBOT_SESSION_DIR.
SESSION_DIR = os.environ.get(
    "NINEBOT_SESSION_DIR",
    os.path.join(os.path.expanduser("~"), ".hermes", "secrets"),
)
SESSION_FILE = os.path.join(SESSION_DIR, "ninebot_session.json")


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------


def _strip_proxy_env() -> None:
    """Drop proxy variables so requests go straight out.

    A local mihomo/clash instance is common on self-hosted boxes, but the
    Ninebot domains are not in its no_proxy allowlist, so traffic goes out
    through the proxy and the peer resets the connection. Direct egress
    against the Ninebot endpoints is both faster and more reliable.
    """
    for var in (
        "http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
        "all_proxy", "ALL_PROXY",
    ):
        os.environ.pop(var, None)


def deep_get(data: Any, path: str) -> Any:
    cur = data
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


def http_request(
    method: str,
    url: str,
    headers: Optional[Dict[str, str]] = None,
    payload: Optional[Dict[str, Any]] = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> Dict[str, Any]:
    headers = dict(headers or {})
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as err:
        body = err.read().decode("utf-8", errors="replace")
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            raise
    except urllib.error.URLError as err:
        raise RuntimeError(f"network error: {err.reason}") from err
    return json.loads(body)


def request_with_retry(method: str, url: str, **kwargs: Any) -> Dict[str, Any]:
    """Retry only transport-level failures; business errors are returned as-is."""
    last: Optional[Exception] = None
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            return http_request(method, url, **kwargs)
        except (urllib.error.URLError, RuntimeError, ConnectionError, TimeoutError) as err:
            last = err
            msg = str(err).lower()
            transient = any(
                k in msg
                for k in ("reset", "timeout", "timed out", "eof", "refused", "temporarily")
            )
            if attempt < RETRY_ATTEMPTS and transient:
                time.sleep(RETRY_BACKOFF)
                continue
            raise
    raise RuntimeError(str(last))


# --------------------------------------------------------------------------
# Config loading
# --------------------------------------------------------------------------


def load_config(path: str) -> Dict[str, Any]:
    """Load a config file. Only ever an explicitly supplied path."""
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def merge_config(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """Shallow-merge two levels deep (section -> keys)."""
    out = dict(base)
    for section, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(section), dict):
            merged = dict(out[section])
            merged.update(value)
            out[section] = merged
        else:
            out[section] = value
    return out


def resolve_api_key(cfg: Dict[str, Any]) -> Optional[str]:
    """Read the key from the environment only.

    Deliberately no --api-key flag and no config.json key: both leak the
    credential into shell history / process listings / world-readable files.
    """
    return os.environ.get("NINEBOT_DEVICESERVICE_KEY") or None


# --------------------------------------------------------------------------
# Session handling (account backend)
# --------------------------------------------------------------------------


def load_session() -> Optional[Dict[str, Any]]:
    if not os.path.exists(SESSION_FILE):
        return None
    try:
        with open(SESSION_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) and data.get("access_token") else None


def save_session(session: Dict[str, Any]) -> None:
    os.makedirs(SESSION_DIR, mode=0o700, exist_ok=True)
    os.chmod(SESSION_DIR, 0o700)
    tmp = SESSION_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(session, f, ensure_ascii=False, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, SESSION_FILE)
    os.chmod(SESSION_FILE, 0o600)


# --------------------------------------------------------------------------
# Backend: official Device Service
# --------------------------------------------------------------------------


def official_list_devices(cfg: Dict[str, Any], api_key: str, lang: str) -> List[Dict[str, Any]]:
    url = cfg["base_url"].rstrip("/") + cfg["devices"]["path"]
    headers: Dict[str, str] = {}
    auth = cfg.get("auth") or {}
    header_name = auth.get("api_key_header") or "Authorization"
    prefix = auth.get("api_key_prefix") or ""
    headers[header_name] = f"{prefix}{api_key}"
    res = request_with_retry(cfg["devices"]["method"], url, headers=headers, payload={} if cfg["devices"]["method"].upper() == "POST" else None)
    return deep_get(res, cfg["devices"]["list_path"]) or []


def official_device_info(cfg: Dict[str, Any], api_key: str, sn: str) -> Dict[str, Any]:
    path = cfg["device_info"]["path"].replace("{sn}", urllib.parse.quote(sn))
    url = cfg["base_url"].rstrip("/") + path
    payload = {k: (v.format(sn=sn) if isinstance(v, str) else v) for k, v in (cfg["device_info"].get("payload") or {}).items()}
    headers: Dict[str, str] = {}
    auth = cfg.get("auth") or {}
    header_name = auth.get("api_key_header") or "Authorization"
    prefix = auth.get("api_key_prefix") or ""
    headers[header_name] = f"{prefix}{api_key}"
    res = request_with_retry(cfg["device_info"]["method"], url, headers=headers, payload=payload)
    if res.get("code") != 0:
        raise RuntimeError(f"API error: {res.get('msg') or res.get('desc') or 'unknown'} (code: {res.get('code')})")
    out = {
        "battery": deep_get(res, cfg["device_info"]["battery_path"]),
        "status": deep_get(res, cfg["device_info"]["status_path"]),
        "location": deep_get(res, cfg["device_info"]["location_path"]),
    }
    for k, path in (cfg["device_info"].get("extra_fields") or {}).items():
        out[k] = deep_get(res, path)
    return out


# --------------------------------------------------------------------------
# Backend: account (pure-Python NET encrypted business channel)
#
# HISTORY / CORRECTION (2026-11-04, two corrections):
#
# 1) The first implementation spoke `POST /app-api/inner/device/ai/*` with a
#    Bearer passport token, copied from the PHP project
#    PrintNow/ninebot-monitor. That route family NO LONGER EXISTS — verified
#    live against Ninebot:
#
#      POST https://cn-cbu-gateway.ninebot.com/app-api/inner/device/ai/get-device-list
#      -> HTTP 404 {"code":1,"message":"Server error","status":404,
#                   "requestId":"oms-device-api-route-ai:..."}
#
#    Ninebot retired the account-password API family in March 2026; only the
#    OpenClaw Device Service (API key) route survived. The ha_ninebot author
#    documented the same shutdown.
#
# 2) The second implementation delegated to the `ninecli` binary. That binary
#    ships ONLY as prebuilt wheels — no sdist, no source — so a credential that
#    can change an account password, bind a phone number and reach the shop
#    would have been handed to code nobody can read. Dropped.
#
# What survives is the encrypted business channel the official app uses:
#
#   POST ebike.ninebot.com/vehicle/binding/my-vehicle         (vehicle list)
#   POST ebike.ninebot.com/vehicle/vehicle/desktop-component   (status)
#   POST steeldust.ninebot.com/v6/vehicle/battery-info         (battery)
#
# Verified live: those hosts answer HTTP 200 with code 340000 / (4102) when no
# identity is attached — the route is alive and only the credential is missing.
#
# The NET encryption (AES-128-CBC + RSA-1024 + key derivation) is implemented
# in `ninebot_net.py`, ported from the MIT-licensed reference implementation
# in likz0421/ninebot-go. Verified against the live gateway: a request built
# by that code is decrypted by Ninebot, and the response is decrypted back.
# --------------------------------------------------------------------------

def _account_env(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Resolve the account credentials: passport token + business uid."""
    session = load_session()
    if not session:
        raise RuntimeError("没有已保存的九号会话")
    token = str(session.get("access_token") or "")
    if not token:
        raise RuntimeError("会话里没有 access_token")
    uid = str(session.get("biz_uid") or "")
    if not uid:
        uid = client.business_login(token) or ""
    return {"token": token, "uid": uid}


def account_list_devices(cfg: Dict[str, Any], access_token: str, lang: str,
                         include_media: bool = False) -> List[Dict[str, Any]]:
    """List the vehicles on the account.

    `include_media` adds the official model picture URL and the colour
    name for the render board. It is opt-in: the MCP surface keeps
    projecting to sn + name only, so no agent transcript ever carries
    asset URLs.
    """
    creds = _account_env(cfg)
    env = client.my_vehicle(creds["token"], creds["uid"])
    code = env.get("code")
    if code not in (0, 1, "0", "1", None):
        raise RuntimeError(
            "九号接口报错: %s (code: %s)" % (env.get("desc") or "unknown", code))
    items: List[Any] = []
    if isinstance(env.get("data"), list):
        items = env["data"]
    elif isinstance(env.get("data"), dict):
        for key in ("vehicleList", "list", "vehicles", "data"):
            if isinstance(env["data"].get(key), list):
                items = env["data"][key]
                break
    out: List[Dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        # The serial rides in `wnumber`; `sn` is an accepted fallback.
        sn = (item.get("wnumber") or item.get("sn") or item.get("vehicleSn")
              or item.get("vehicle_sn"))
        # The account channel spells the name `ble_name` / `device_name` /
        # `vehicle_name`; the official Device Service uses `deviceName`.
        name = (item.get("ble_name") or item.get("device_name")
                or item.get("vehicle_name") or item.get("vehicleName")
                or item.get("name") or item.get("nick")
                or item.get("nickname") or item.get("alias") or item.get("title"))
        if sn:
            row: Dict[str, Any] = {"sn": str(sn), "name": str(name) if name else ""}
            if include_media:
                # The official model picture for this exact trim, plus the
                # colour name. Both live on the vehicle record; the signed
                # URL must be read at runtime, never hardcoded.
                row["imgUrl"] = str(
                    item.get("img_url") or item.get("v6_light_img_url") or "")
                row["color"] = str(item.get("color") or "")
                # The odometer reading, when the account is allowed one.
                # Null on a shared account — that is a permission edge,
                # not a missing feature.
                row["totalMileage"] = item.get("total_mileage")
            out.append(row)
    return out


def account_battery_info(cfg: Dict[str, Any], access_token: str,
                          sn: str) -> Dict[str, Any]:
    """Battery-pack detail: voltage, temperature, cycles, charge power.

    The ebike gateway answers for an Fz-series e-bike; the motor gateway is
    the fallback (the client tries both).
    """
    creds = _account_env(cfg)
    env = client.battery_info(creds["token"], creds["uid"], sn)
    code = env.get("code")
    if code not in (0, 1, "0", "1", None):
        raise RuntimeError(
            "九号接口报错: %s (code: %s)" % (env.get("desc") or "unknown", code))
    data = env.get("data")
    if isinstance(data, list):
        data = data[0] if data else {}
    if not isinstance(data, dict):
        return {}
    # The pack list carries the per-pack readings; the top level repeats
    # the main pack's values.
    packs = data.get("battery_list") or data.get("batlist") or []
    main = packs[0] if isinstance(packs, list) and packs and isinstance(packs[0], dict) else {}
    # `have_bms_cycle_support` says whether the pack's cycle counter is
    # real. When it is False the gateway still reports bms_cycle, but it
    # is a placeholder (100), not a count — so drop it rather than
    # print a number that is not true.
    has_cycle = bool(data.get("have_bms_cycle_support"))
    cycles = (main.get("bms_cycle") if has_cycle else None)
    return {
        "battery": data.get("electricity") or main.get("electricity"),
        "hasCycleSupport": has_cycle,
        "voltage": main.get("bms_volt") or main.get("bmsvolt"),
        "temperature": main.get("bat_temp") or main.get("battemp"),
        "cycles": cycles,
        "health": main.get("score"),
        "batteryType": data.get("battery_type"),
        "isLithium": data.get("is_li"),
        "chargingPower": data.get("charging_power"),
        "remainChargeTime": data.get("remain_charge_time"),
        "estimateMileage": data.get("estimate_mil"),
        "packCount": data.get("battery_count"),
    }


def account_device_info(cfg: Dict[str, Any], access_token: str, sn: str) -> Dict[str, Any]:
    creds = _account_env(cfg)
    env = client.desktop_component(creds["token"], creds["uid"], sn)
    code = env.get("code")
    if code not in (0, 1, "0", "1", None):
        raise RuntimeError(
            "九号接口报错: %s (code: %s)" % (env.get("desc") or "unknown", code))

    # `data` is either a list or an object; the reference client takes the
    # leading object.
    payload = env.get("data")
    data: Dict[str, Any] = {}
    if isinstance(payload, dict):
        data = payload
    elif isinstance(payload, list) and payload and isinstance(payload[0], dict):
        data = payload[0]

    def pick(*keys: str) -> Any:
        for k in keys:
            if k in data and data[k] is not None:
                return data[k]
        return None

    # The location is a nested object: {"lat":..., "lon":..., "lock":...}.
    # There is no human-readable address on this endpoint.
    loc = pick("loc", "location", "locationInfo")
    location_text: Any = None
    lat: Any = None
    lon: Any = None
    lock_state: Any = None
    if isinstance(loc, dict):
        lat = loc.get("lat")
        lon = loc.get("lon")
        lock_state = loc.get("lock")
        location_text = loc.get("desc") or loc.get("address")
    elif loc is not None:
        location_text = loc

    # The Ninebot API gives coordinates only. Turn them into a readable
    # Chinese place via a geocoder (Nominatim / Amap / QWeather), cached.
    location_detail = None
    location_source = None
    if lat is not None and lon is not None:
        try:
            found = geo.reverse_geocode(lat, lon)
        except Exception:  # noqa: BLE001 - never fail a status read on geo
            found = None
        if found:
            # Keep whatever the Ninebot API itself reported if it has any.
            location_text = found.get("short") or found.get("address") or location_text
            location_detail = found.get("address")
            location_source = found.get("source")

    return {
        # snake_case on this channel — NOT the camelCase of the official
        # Device Service (dumpEnergy / powerStatus / locationInfo.*).
        "battery": pick("dump_energy", "battery", "batteryLevel"),
        "status": pick("pwr", "power_status", "powerStatus", "status"),
        "location": location_text,
        "locationDetail": location_detail,
        "locationSource": location_source,
        "lat": lat,
        "lon": lon,
        "lock": lock_state,
        # The account channel spells it `estimate_mileage`; the official
        # Device Service uses `estimateMileage`. Accept both.
        "estimateMileage": pick("estimate_mileage", "range",
                                "estimateMileage", "mileage"),
        "chargingState": pick("charging", "chargingState"),
        "ble_name": pick("ble_name", "device_name", "vehicle_name"),
        # Extra fields the vehicle endpoint reports but the Device Service
        # does not have.
        "aiEstimateMileage": pick("ai_estimate_mileage"),
        "preciseEstimateMileage": pick("precise_estimate_mileage"),
        "remainChargeTime": pick("remain_charge_time"),
        "smartServiceExpired": pick("is_smart_service_expired"),
        "barrelLock": pick("barrel_lock_status"),
        "permissions": pick("permissions"),
    }


# --------------------------------------------------------------------------
# Backend selection
# --------------------------------------------------------------------------


def _ninecli_has_tokens() -> bool:
    """Deprecated shim — kept so callers do not break on an import.

    The account backend no longer uses the ninecli binary (it ships only
    prebuilt wheels, no source). Credentials live in our own session file.
    """
    return False


def pick_backend(explicit: Optional[str], cfg: Dict[str, Any]) -> str:
    if explicit:
        return explicit
    backend = cfg.get("backend")
    if backend in ("official", "account"):
        return backend
    if resolve_api_key(cfg):
        return "official"
    if load_session() or _ninecli_has_tokens():
        return "account"
    return "account"


# --------------------------------------------------------------------------
# Formatting
# --------------------------------------------------------------------------

POWER_MAP = {"0": "OFF", "1": "ON", "OFF": "OFF", "ON": "ON"}


def _business_uid(cfg: Dict[str, Any]) -> str:
    """The short business uid the vehicle APIs expect.

    It lives in the session file, not in the static config — reading it
    from the config yields an empty uid, and the travel gateway answers
    an empty uid with "通讯异常(4200)" rather than an obvious error.
    """
    uid = str((cfg or {}).get("biz_uid") or "")
    if uid:
        return uid
    session = load_session() or {}
    return str(session.get("biz_uid") or "")


def account_travel_list(cfg: Dict[str, Any], access_token: str, sn: str,
                        month: Optional[str] = None) -> Dict[str, Any]:
    """Ride history for one month.

    Returns the gateway's shape: month, times, total_mileages, duration,
    ec, detail[] (per-day) and list[] (per-ride).
    """
    import datetime as _dt
    if not month:
        month = _dt.date.today().strftime("%Y%m")
    # The travel gateway intermittently answers with code=1 but desc
    # "通讯异常(4200)" — a transient upstream hiccup, not an empty month.
    # Retry it; only report the error once the retries are exhausted.
    env: Dict[str, Any] = {}
    for attempt in range(3):
        env = client.travel_list(access_token, _business_uid(cfg), sn, month)
        if env.get("code") == 1 and "4200" not in str(env.get("desc") or ""):
            break
        time.sleep(1.0 + attempt)
    if env.get("code") != 1:
        return {"month": month, "times": 0, "total_mileages": "0.0",
                "duration": 0, "ec": 0, "list": [], "detail": [],
                "error": env.get("desc")}
    data = env.get("data") or {}
    return {
        "month": data.get("month") or month,
        "times": data.get("times") or 0,
        "total_mileages": data.get("total_mileages") or "0.0",
        "duration": data.get("duration") or 0,
        "ec": data.get("ec") or 0,
        "first_time": data.get("first_time"),
        "list": data.get("list") or [],
        "detail": data.get("detail") or [],
    }


TRAVEL_MONTH_CACHE = os.path.expanduser(
    os.environ.get("NINEBOT_TRAVEL_CACHE",
                   "~/.hermes/cache/ninebot_travel_months.json"))


def _month_cache_load() -> Dict[str, Any]:
    try:
        with open(TRAVEL_MONTH_CACHE, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _month_cache_store(data: Dict[str, Any]) -> None:
    try:
        os.makedirs(os.path.dirname(TRAVEL_MONTH_CACHE), exist_ok=True)
        tmp = TRAVEL_MONTH_CACHE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, TRAVEL_MONTH_CACHE)
    except OSError:
        pass


def _month_is_past(month: str) -> bool:
    import datetime as _dt
    return month < _dt.date.today().strftime("%Y%m")


def travel_months(cfg: Dict[str, Any], access_token: str, sn: str,
                  months: List[str]) -> Dict[str, Dict[str, Any]]:
    """Fetch several months of summaries, caching the finished ones.

    A past month never changes, so it is fetched once and then read from
    disk; only the current month is re-queried. Without this, a year view
    would cost twelve encrypted round-trips on every run.
    """
    uid = _business_uid(cfg)
    cache = _month_cache_load()
    out: Dict[str, Dict[str, Any]] = {}
    dirty = False
    for month in months:
        if _month_is_past(month) and isinstance(cache.get(month), dict):
            out[month] = cache[month]
            continue
        try:
            env = client.travel_list(access_token, uid, sn, month)
        except Exception:  # noqa: BLE001 - one bad month must not kill the view
            out[month] = {"month": month, "times": 0, "total_mileages": "0.0",
                          "duration": 0, "ec": 0, "list": []}
            continue
        data = env.get("data") or {}
        if env.get("code") != 1:
            out[month] = {"month": month, "times": 0, "total_mileages": "0.0",
                          "duration": 0, "ec": 0, "list": []}
            continue
        rec = {
            "month": data.get("month") or month,
            "times": data.get("times") or 0,
            "total_mileages": data.get("total_mileages") or "0.0",
            "duration": data.get("duration") or 0,
            "ec": data.get("ec") or 0,
            "list": data.get("list") or [],
        }
        out[month] = rec
        if _month_is_past(month):
            cache[month] = rec
            dirty = True
    if dirty:
        _month_cache_store(cache)
    return out


def travel_mileage_summary(cfg: Dict[str, Any], access_token: str, sn: str,
                            current_month: str) -> Dict[str, Any]:
    """Day / week / month / year / all-time mileage from ride records.

    All figures are sums over rides the account can see. The vehicle's own
    odometer is not exposed to a shared account (`total_mileage` comes back
    null), so "all-time" is the cached range, not the bike's ODO.
    """
    import datetime as _dt

    today = _dt.date.today()
    year = today.strftime("%Y")
    months = [f"{year}{m:02d}" for m in range(1, int(today.strftime("%m")) + 1)]
    data = travel_months(cfg, access_token, sn, months)

    def _km(rec: Dict[str, Any]) -> float:
        try:
            return float(rec.get("total_mileages") or 0)
        except (TypeError, ValueError):
            return 0.0

    month_km = _km(data.get(current_month, {}))
    year_km = sum(_km(r) for r in data.values())
    year_times = sum(int(r.get("times") or 0) for r in data.values())

    # Day and week come from the rides themselves — the month summary has
    # no per-day breakdown we can trust the indexing of.
    rides: List[Dict[str, Any]] = []
    for rec in data.values():
        rides.extend(rec.get("list") or [])

    def _day(ride: Dict[str, Any]) -> str:
        stamp = str(ride.get("end_time_format") or "")
        return stamp[:10]

    week_start = today - _dt.timedelta(days=today.weekday())   # Monday
    today_s = today.strftime("%Y-%m-%d")
    week_s = week_start.strftime("%Y-%m-%d")

    day_km = sum(float(r.get("mileages") or 0) for r in rides if _day(r) == today_s)
    day_times = sum(1 for r in rides if _day(r) == today_s)
    week_km = sum(float(r.get("mileages") or 0) for r in rides if _day(r) >= week_s)
    week_times = sum(1 for r in rides if _day(r) >= week_s)

    # Everything we have ever fetched: the cached past months PLUS the
    # current one (which is deliberately never cached, since it still
    # changes). Summing the cache alone silently drops this month.
    cache = _month_cache_load()
    seen: Dict[str, Dict[str, Any]] = dict(cache)
    seen[current_month] = data.get(current_month) or {}
    all_km = sum(_km(r) for r in seen.values())
    all_times = sum(int(r.get("times") or 0) for r in seen.values())
    # "Since" is the first month that actually has rides, not the first
    # month we happened to query (empty months get cached too).
    with_rides = sorted(m for m, r in seen.items() if int(r.get("times") or 0) > 0)
    first_month = with_rides[0] if with_rides else current_month

    return {
        "day": {"km": round(day_km, 1), "times": day_times},
        "week": {"km": round(week_km, 1), "times": week_times,
                 "since": week_s},
        "month": {"km": round(month_km, 1),
                  "times": int(data.get(current_month, {}).get("times") or 0),
                  "label": current_month},
        "year": {"km": round(year_km, 1), "times": year_times, "label": year},
        "all": {"km": round(all_km, 1), "times": all_times,
                "since": first_month},
        "odometerAvailable": False,
    }


def account_travel_detail(cfg: Dict[str, Any], access_token: str, sn: str,
                           travel_id: str) -> Dict[str, Any]:
    """One ride in full (distance, energy, GPS trail)."""
    env = client.travel_detail(access_token, _business_uid(cfg), sn, travel_id)
    if env.get("code") != 1:
        return {"error": env.get("desc")}
    return env.get("data") or {}


def normalize_power(value: Any) -> str:
    if value is None:
        return "unknown"
    return POWER_MAP.get(str(value).upper(), "unknown")


def normalize_charging(value: Any) -> str:
    """Normalise the charge flag.

    The business channel reports `charging` as a boolean or a 0/1 int; the
    official Device Service uses "1"/"0". Accept both.
    """
    if value is None:
        return "unknown"
    if isinstance(value, bool):
        return "charging" if value else "not_charge"
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "charging", "in"):
        return "charging"
    if text in ("0", "false", "no", "not_charge", "out"):
        return "not_charge"
    return "unknown"


def project_device(device: Dict[str, Any], sn_field: str, name_field: str) -> Dict[str, str]:
    """Only sn + name — never the whole upstream object.

    Upstream device records carry extra metadata (image data, and possibly
    account-scoped fields in future versions). Dumping them all into an
    agent transcript is unnecessary data exposure.

    Both backends spell the serial and the name differently — the official
    Device Service uses `sn` / `deviceName`, the account channel uses
    `wnumber` / `ble_name` / `device_name` / `vehicle_name`. Fall back
    across the alternatives so a rename on either side does not blank the
    output.
    """
    sn = ""
    for key in (sn_field, "wnumber", "sn", "sn_str"):
        value = device.get(key)
        if value:
            sn = str(value)
            break
    name = ""
    for key in (name_field, "ble_name", "device_name", "vehicle_name",
                "deviceName", "name"):
        value = device.get(key)
        if value:
            name = str(value)
            break
    return {"sn": sn, "name": name}


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Query Ninebot (九号) vehicle status.",
        epilog="The API key is read from NINEBOT_DEVICESERVICE_KEY only.",
    )
    parser.add_argument("--backend", choices=["official", "account"],
                        help="force a backend (default: auto-detect)")
    parser.add_argument("--config", default=None,
                        help="explicit config JSON path (never auto-discovered)")
    parser.add_argument("--device-name", default=None, help="device name to select")
    parser.add_argument("--device-sn", default=None, help="device SN to select")
    parser.add_argument("--lang", default="zh", help="language: zh | zh-hant | en")
    parser.add_argument("--raw", action="store_true",
                        help="emit the raw upstream payload instead of the normalised shape")
    args = parser.parse_args()

    _strip_proxy_env()

    cfg = ACCOUNT_CONFIG
    if args.backend == "official":
        cfg = OFFICIAL_CONFIG
    if args.config:
        cfg = merge_config(cfg, load_config(args.config))

    backend = pick_backend(args.backend, cfg)
    if backend == "official":
        cfg = merge_config(OFFICIAL_CONFIG, {k: v for k, v in cfg.items() if k in ("base_url", "auth", "devices", "device_info")})
    else:
        cfg = merge_config(ACCOUNT_CONFIG, {k: v for k, v in cfg.items() if k in ("base_url", "auth", "devices", "device_info")})

    if backend == "official":
        api_key = resolve_api_key(cfg)
        if not api_key:
            print(json.dumps({
                "error": "official backend needs NINEBOT_DEVICESERVICE_KEY in the environment",
                "hint": "报名九号 OpenClaw 内测获取 Key（九号出行 APP「圈子」），然后 export NINEBOT_DEVICESERVICE_KEY=...",
            }, ensure_ascii=False))
            return 2
        devices = official_list_devices(cfg, api_key, args.lang)
    else:
        session = load_session()
        if not session:
            print(json.dumps({
                "error": "account backend needs a Ninebot session",
                "hint": ("run `python3 scripts/ninebot_login.py --user <phone>` "
                         "in an interactive terminal first (it saves the "
                         "session), or use the official backend with a "
                         "Device Service key"),
            }, ensure_ascii=False))
            return 2
        devices = account_list_devices(cfg, session.get("access_token", ""), args.lang)

    if not devices:
        print(json.dumps({"error": "no devices found for this account"}, ensure_ascii=False))
        return 2

    sn_field = cfg["devices"]["sn_field"]
    name_field = cfg["devices"]["name_field"]

    selected = None
    if args.device_sn:
        for d in devices:
            if str(d.get(sn_field)) == args.device_sn:
                selected = d
                break
    elif args.device_name:
        for d in devices:
            if str(d.get(name_field)) == args.device_name:
                selected = d
                break
    else:
        if len(devices) > 1:
            print(json.dumps({
                "choose_device": [project_device(d, sn_field, name_field) for d in devices]
            }, ensure_ascii=False))
            return 3
        selected = devices[0]

    if not selected:
        print(json.dumps({
            "error": "device not found",
            "devices": [project_device(d, sn_field, name_field) for d in devices],
        }, ensure_ascii=False))
        return 4

    sn = str(selected.get(sn_field))
    name = selected.get(name_field) or ""
    if not name:
        # The account channel spells the name ble_name / device_name /
        # vehicle_name rather than the official backend's deviceName.
        for key in ("ble_name", "device_name", "vehicle_name", "name"):
            value = selected.get(key)
            if value:
                name = str(value)
                break

    if backend == "official":
        info = official_device_info(cfg, api_key, sn)
    else:
        info = account_device_info(cfg, session.get("access_token", ""), sn)
        # Voltage and temperature live on the battery endpoint only.
        try:
            batt = account_battery_info(cfg, session.get("access_token", ""), sn)
        except Exception:  # noqa: BLE001 - status must not fail on battery
            batt = {}
        for key in ("voltage", "temperature", "cycles", "health",
                    "chargingPower", "batteryType", "isLithium"):
            if batt.get(key) is not None:
                info[key] = batt[key]

    if args.raw:
        print(json.dumps(info, ensure_ascii=False))
        return 0

    output = {
        "device_name": name,
        "battery": info.get("battery"),
        "powerStatus": normalize_power(info.get("status")),
        "chargingState": normalize_charging(info.get("chargingState")),
        "location": info.get("location"),
        "lat": info.get("lat"),
        "lon": info.get("lon"),
        "lock": info.get("lock"),
        "estimateMileage": info.get("estimateMileage"),
    }
    # Fields only the account channel reports.
    if info.get("aiEstimateMileage") is not None:
        output["aiEstimateMileage"] = info.get("aiEstimateMileage")
    if info.get("preciseEstimateMileage") is not None:
        output["preciseEstimateMileage"] = info.get("preciseEstimateMileage")
    if info.get("remainChargeTime") not in (None, ""):
        output["remainChargeTime"] = info.get("remainChargeTime")
    if info.get("smartServiceExpired") is not None:
        output["smartServiceExpired"] = info.get("smartServiceExpired")
    if info.get("permissions"):
        output["permissions"] = info.get("permissions")
    for key in ("voltage", "temperature", "cycles", "health", "chargingPower",
                "batteryType", "isLithium", "barrelLock"):
        if info.get(key) is not None:
            output[key] = info[key]
    print(json.dumps(output, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except urllib.error.HTTPError as err:
        body = ""
        try:
            body = err.read().decode("utf-8", errors="replace")[:400]
        except Exception:
            pass
        print(json.dumps({"error": f"HTTP {err.code}", "body": body}, ensure_ascii=False))
        sys.exit(1)
    except RuntimeError as err:
        print(json.dumps({"error": str(err)}, ensure_ascii=False))
        sys.exit(1)
