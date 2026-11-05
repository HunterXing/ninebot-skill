#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MCP server exposing Ninebot vehicle queries as typed tools.

Stdlib only — the MCP protocol is implemented directly over stdio JSON-RPC,
so this has no pip dependency and runs anywhere Python 3.8+ does.

Tools:
    list_devices     list the vehicles on the account
    vehicle_status   battery / power / charging / location / range for one vehicle
    vehicle_battery  battery detail for one vehicle
    backend_info     which auth backend is active and whether it is usable

Read-only by design: no control tools are exposed.
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ninebot_query as nq  # noqa: E402

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "ninebot-skill", "version": "1.0.0"}

TOOLS: List[Dict[str, Any]] = [
    {
        "name": "list_devices",
        "description": "List the Ninebot vehicles on the configured account (sn + display name only).",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "vehicle_status",
        "description": (
            "Get one Ninebot vehicle's live state: battery %, power on/off, charging state, "
            "location description and estimated range in km."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "sn": {"type": "string", "description": "Vehicle serial number (preferred)."},
                "device_name": {"type": "string", "description": "Vehicle display name (fallback)."},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "vehicle_battery",
        "description": "Get one Ninebot vehicle's battery detail (percentage, voltage, temperature, health).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "sn": {"type": "string", "description": "Vehicle serial number (preferred)."},
                "device_name": {"type": "string", "description": "Vehicle display name (fallback)."},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "vehicle_travel",
        "description": "Get one Ninebot vehicle's ride history for a month (count, distance, duration, per-ride detail).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "sn": {"type": "string", "description": "Vehicle serial number (preferred)."},
                "device_name": {"type": "string", "description": "Vehicle display name (fallback)."},
                "month": {"type": "string", "description": "Month as YYYYMM. Defaults to the current month."},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "backend_info",
        "description": "Report which Ninebot auth backend is active and whether it is ready to query.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
]


def _session() -> Dict[str, Any]:
    """Load the saved account session, or raise a clear error."""
    session = nq.load_session()
    if not session:
        raise RuntimeError("account backend needs a saved session — run scripts/ninebot_login.py")
    return session


def _load_cfg(backend: Optional[str]) -> Dict[str, Any]:
    base = nq.OFFICIAL_CONFIG if backend == "official" else nq.ACCOUNT_CONFIG
    return base


def _resolve_devices(cfg: Dict[str, Any], backend: str) -> List[Dict[str, Any]]:
    if backend == "official":
        key = nq.resolve_api_key(cfg)
        if not key:
            raise RuntimeError("official backend needs NINEBOT_DEVICESERVICE_KEY")
        return nq.official_list_devices(cfg, key, "zh")
    return nq.account_list_devices(cfg, _session()["access_token"], "zh")


def _pick_device(devices: List[Dict[str, Any]], cfg: Dict[str, Any],
                 sn: Optional[str], device_name: Optional[str]) -> Dict[str, Any]:
    sn_field = cfg["devices"]["sn_field"]
    name_field = cfg["devices"]["name_field"]
    if sn:
        for d in devices:
            if str(d.get(sn_field)) == sn:
                return d
    elif device_name:
        for d in devices:
            if str(d.get(name_field)) == device_name:
                return d
    else:
        if len(devices) == 1:
            return devices[0]
        raise RuntimeError(
            "multiple vehicles — specify sn or device_name; available: "
            + json.dumps(
                [nq.project_device(d, sn_field, name_field) for d in devices],
                ensure_ascii=False,
            )
        )
    raise RuntimeError("vehicle not found")


def tool_list_devices(args: Dict[str, Any]) -> Dict[str, Any]:
    backend = nq.pick_backend(args.get("backend"), _load_cfg(None))
    cfg = _load_cfg(backend)
    devices = _resolve_devices(cfg, backend)
    sn_field = cfg["devices"]["sn_field"]
    name_field = cfg["devices"]["name_field"]
    return {"vehicles": [nq.project_device(d, sn_field, name_field) for d in devices]}


def tool_vehicle_status(args: Dict[str, Any]) -> Dict[str, Any]:
    backend = nq.pick_backend(args.get("backend"), _load_cfg(None))
    cfg = _load_cfg(backend)
    devices = _resolve_devices(cfg, backend)
    dev = _pick_device(devices, cfg, args.get("sn"), args.get("device_name"))
    sn = str(dev.get(cfg["devices"]["sn_field"]))
    if backend == "official":
        key = nq.resolve_api_key(cfg)
        if not key:
            raise RuntimeError("official backend needs NINEBOT_DEVICESERVICE_KEY")
        info = nq.official_device_info(cfg, key, sn)
    else:
        info = nq.account_device_info(cfg, _session()["access_token"], sn)
    out = {
        "device_name": dev.get(cfg["devices"]["name_field"]) or "",
        "sn": sn,
        "battery": info.get("battery"),
        "powerStatus": nq.normalize_power(info.get("status")),
        "chargingState": nq.normalize_charging(info.get("chargingState")),
        "location": info.get("location"),
        "locationDetail": info.get("locationDetail"),
        "locationSource": info.get("locationSource"),
        "estimateMileage": info.get("estimateMileage"),
        "remainChargingTime": info.get("remainChargeTime"),
        "lat": info.get("lat"),
        "lon": info.get("lon"),
        "lock": info.get("lock"),
        "smartServiceExpired": info.get("smartServiceExpired"),
        "permissions": info.get("permissions"),
    }
    if backend != "official":
        # Voltage and temperature only exist on the battery endpoint.
        try:
            batt = nq.account_battery_info(cfg, _session()["access_token"], sn)
            for key_name in ("voltage", "temperature", "cycles", "health"):
                if batt.get(key_name) is not None:
                    out[key_name] = batt[key_name]
        except Exception:  # noqa: BLE001 - status must not fail on battery
            pass
        # The native odometer lives on `vehicle-info`, not on the status
        # endpoint — and NOT on the vehicle-list record, whose
        # `total_mileage` is null for a shared account. Missing stays
        # missing: a null odometer is not "0 km".
        try:
            vinfo = nq.account_vehicle_info(cfg, _session()["access_token"], sn)
            if vinfo.get("odometerKm") is not None:
                out["totalMileage"] = vinfo["odometerKm"]
            if vinfo.get("identificationNumber"):
                out["identificationNumber"] = vinfo["identificationNumber"]
        except Exception:  # noqa: BLE001 - status must not fail on profile
            pass
    return out


def tool_vehicle_battery(args: Dict[str, Any]) -> Dict[str, Any]:
    backend = nq.pick_backend(args.get("backend"), _load_cfg(None))
    cfg = _load_cfg(backend)
    devices = _resolve_devices(cfg, backend)
    dev = _pick_device(devices, cfg, args.get("sn"), args.get("device_name"))
    sn = str(dev.get(cfg["devices"]["sn_field"]))
    out = {
        "device_name": dev.get(cfg["devices"]["name_field"]) or "",
        "sn": sn,
    }
    if backend == "official":
        key = nq.resolve_api_key(cfg)
        if not key:
            raise RuntimeError("official backend needs NINEBOT_DEVICESERVICE_KEY")
        info = nq.official_device_info(cfg, key, sn)
        out["battery"] = info.get("battery")
        for key_name in ("estimateMileage", "remainChargingTime"):
            if info.get(key_name) is not None:
                out[key_name] = info[key_name]
        return out

    session = _session()
    out["battery"] = None
    try:
        batt = nq.account_battery_info(cfg, session["access_token"], sn)
    except Exception as exc:  # noqa: BLE001
        batt = {}
        out["batteryError"] = str(exc)
    for key_name in ("battery", "voltage", "temperature", "cycles", "health",
                     "batteryType", "isLithium", "chargingPower",
                     "remainChargeTime", "estimateMileage", "packCount"):
        if batt.get(key_name) is not None:
            out[key_name] = batt[key_name]
    return out


def tool_vehicle_travel(args: Dict[str, Any]) -> Dict[str, Any]:
    """Ride history for one vehicle, one month."""
    backend = nq.pick_backend(args.get("backend"), _load_cfg(None))
    if backend != "account":
        raise RuntimeError("ride history is only available on the account backend")
    cfg = _load_cfg(None)
    session = _session()
    devices = _resolve_devices(cfg, backend)
    device = _pick_device(devices, cfg, args.get("sn"), args.get("device_name"))
    sn = str(device.get("sn") or device.get("wnumber") or "")
    if not sn:
        raise RuntimeError("vehicle has no serial number")
    month = (args.get("month") or "").strip() or None
    data = nq.account_travel_list(cfg, session["access_token"], sn, month)
    rides = list(data.get("list") or [])
    rides.sort(key=lambda r: int(r.get("end_time") or 0), reverse=True)
    data["list"] = rides
    data["device_name"] = device.get("name") or device.get("device_name") or ""
    data["sn"] = sn
    return data


def tool_backend_info(args: Dict[str, Any]) -> Dict[str, Any]:
    has_key = bool(os.environ.get("NINEBOT_DEVICESERVICE_KEY"))
    session = nq.load_session()
    return {
        "active_backend": nq.pick_backend(args.get("backend"), _load_cfg(None)),
        "official_key_present": has_key,
        "account_session_present": bool(session),
        "session_path": nq.SESSION_FILE,
        "session_exists": os.path.exists(nq.SESSION_FILE),
        "note": "official needs NINEBOT_DEVICESERVICE_KEY; account needs a saved session",
    }


HANDLERS = {
    "list_devices": tool_list_devices,
    "vehicle_status": tool_vehicle_status,
    "vehicle_battery": tool_vehicle_battery,
    "vehicle_travel": tool_vehicle_travel,
    "backend_info": tool_backend_info,
}


def send(msg: Dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def handle(msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    method = msg.get("method")
    mid = msg.get("id")

    if method == "initialize":
        return {
            "jsonrpc": "2.0", "id": mid,
            "result": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": SERVER_INFO,
            },
        }
    if method == "notifications/initialized":
        return None
    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}}
    if method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name") or ""
        args = params.get("arguments") or {}
        handler = HANDLERS.get(name)
        if not handler:
            return {"jsonrpc": "2.0", "id": mid,
                    "error": {"code": -32601, "message": f"unknown tool: {name}"}}
        try:
            data = handler(args)
            text = json.dumps(data, ensure_ascii=False, indent=2)
            return {"jsonrpc": "2.0", "id": mid,
                    "result": {"content": [{"type": "text", "text": text}],
                               "isError": False}}
        except Exception as exc:  # noqa: BLE001
            detail = str(exc) or exc.__class__.__name__
            traceback.print_exc(file=sys.stderr)
            text = json.dumps({"error": detail}, ensure_ascii=False, indent=2)
            return {"jsonrpc": "2.0", "id": mid,
                    "result": {"content": [{"type": "text", "text": text}],
                               "isError": True}}
    if mid is not None:
        return {"jsonrpc": "2.0", "id": mid,
                "error": {"code": -32601, "message": f"method not found: {method}"}}
    return None


def main() -> int:
    nq._strip_proxy_env()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        resp = handle(msg)
        if resp is not None:
            send(resp)
    return 0


if __name__ == "__main__":
    sys.exit(main())
