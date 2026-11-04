"""Ninebot business-channel client — pure Python, no binary in the trust chain.

The NET encryption primitives live in `ninebot_net`. This module adds the
request/response plumbing on top: the business login that exchanges a
Passport token for the short business uid, and the vehicle endpoints.

Trust model (2026-11-04)
-------------------------
The password only ever touches the Passport endpoint, over TLS, RSA-encrypted
with the app's public key, by `ninebot_sign` / `ninebot_login` — code in this
repository. `ninecli` (PyPI) would have done the same job, but it ships only
prebuilt wheels with no source, so it was dropped from the chain: a credential
that can change an account password, bind a phone number and reach the shop
should not be handed to a binary nobody can read.

Identity flow
-------------
    Passport login   -> access_token  (19-digit passport uuid)
    business login   -> uid           (short business uid, e.g. 87517671)
    vehicle APIs     -> address the caller by uid, not by the passport uuid

Using the passport uuid where the business uid is expected makes the gateway
answer 4103; passing no uid at all answers 4102.
"""

from __future__ import annotations

import http.client
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from urllib.parse import urlparse
from typing import Any, Dict, List, Optional, Tuple

import ninebot_net as net

# --------------------------------------------------------------------------
# Hosts
# --------------------------------------------------------------------------

PASSPORT_HOST = "https://api-passport-bj.ninebot.com"
BIZ_HOST = "https://api-jhcx-v6-bj.ninebot.com"
EBIKE_HOST = "https://ebike.ninebot.com"
MOTOR_HOST = "https://steeldust.ninebot.com"
TRAVEL_HOST = "https://cn-cbu-gateway.ninebot.com"

# The app's CommonParams table, in the reference's exact emission order.
#
# These values were read off the OFFICIAL CLI binary's live business-channel
# request (captured by pointing its `--biz-host` override at a local fake
# gateway):
#
#   Client-Ver:   610063322
#   Platform-Ver: 13
#   Regionx:      bj
#   User-Agent:   okhttp/4.9.1
#
# Note the split: the `Platform-Ver` HEADER carries the OS version alone
# ("13"), while the `platform_ver` CommonParam carries version + model
# ("13 Xiaomi").
APP_VERSION = "610063322"
DEVICE_ID = os.environ.get("NINEBOT_DEVICE_ID", "9f3c1d7a5b2e4086a1c4d9e7f2b6c083")
OS_VERSION = os.environ.get("NINEBOT_OS_VERSION", "13")
PLATFORM_VER = os.environ.get("NINEBOT_PLATFORM_VER", "13 Xiaomi")
REGION = "bj"
USER_AGENT = "okhttp/4.9.1"

DEFAULT_TIMEOUT = 20
RETRY_ATTEMPTS = 3
RETRY_BACKOFF = 2.0


def strip_proxy_env() -> None:
    """Drop proxy variables so requests go straight out.

    The local mihomo/clash instance does not carry the Ninebot domains in its
    no_proxy allowlist, so proxied traffic is reset by the peer. Direct
    egress is both faster and more reliable.
    """
    for var in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
                "all_proxy", "ALL_PROXY"):
        os.environ.pop(var, None)


# --------------------------------------------------------------------------
# Transport
# --------------------------------------------------------------------------


def _post_encrypted(url: str, extra_params: List[Tuple[str, Any]],
                    access_token: str, uid: str,
                    business_type: str = "1",
                    extra_headers: Optional[Dict[str, str]] = None
                    ) -> Dict[str, Any]:
    """One encrypted business call: build, send, decrypt, unwrap.

    Returns the inner business envelope ({"code":..., "data":..., "desc":...}).

    `extra_headers` exists because some gateways want more than the common
    set — the travel gateway asks for the token and the tracking-library
    headers on top.
    """
    params: List[Tuple[str, Any]] = [
        ("sys_language", "zh-hans-cn"),
        ("client_ver", APP_VERSION),
        ("device_id", DEVICE_ID),
        ("regionx", REGION),
        ("language", "zh"),
        ("ostype", "and"),
        ("lang", "zh"),
        ("platform_ver", PLATFORM_VER),
        ("platform", "android"),
        ("login_country", "CN"),
    ]
    if access_token:
        params.append(("access_token", access_token))
    if uid:
        params.append(("uid", uid))
    params.extend(extra_params)

    inner = net.build_inner(params)
    kd = net.gen_key_data()
    body, kd = net.encrypt_request(inner, kd)

    headers = {
        "Content-Type": "text/html;charset=UTF-8",
        "User-Agent": USER_AGENT,
        "Ninebot-Version": "2",
        "Client-Ver": APP_VERSION,
        "Business-Type": business_type,
        "Language": "zh",
        "Sys-Language": "zh-hans-cn",
        "Login-Country": "CN",
        "Platform": "Android",
        "Platform-Ver": OS_VERSION,
        "Regionx": REGION,
        "Device-Id": DEVICE_ID,
        "Request-Id": os.urandom(16).hex(),
        "Service-Time": str(int(time.time() * 1000)),
        "Cache-Control": "no-cache",
        "Debug": "0",
        "Need_decrypt": "1",
    }
    if uid:
        headers["Uid"] = uid
    if extra_headers:
        headers.update(extra_headers)

    last_exc: Optional[Exception] = None
    parsed = urllib.parse.urlsplit(url)
    host = parsed.hostname or ""
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        conn = None
        try:
            # `http.client`, not `urllib.request`: the latter applies
            # `str.title()` to every header name, which turns
            # `Need_decrypt` into `Need_Decrypt`. The official client (Go)
            # sends `Need_decrypt` — Go's textproto only capitalises after a
            # hyphen. HTTP header names are case-insensitive per RFC, but a
            # server reading them with an exact-match map would miss the
            # flag and skip decrypting the response.
            if parsed.scheme == "https":
                conn = http.client.HTTPSConnection(
                    host, parsed.port or 443, timeout=DEFAULT_TIMEOUT)
            else:
                conn = http.client.HTTPConnection(
                    host, parsed.port or 80, timeout=DEFAULT_TIMEOUT)
            conn.request("POST", parsed.path or "/", body=body, headers=headers)
            resp = conn.getresponse()
            raw = resp.read()
            break
        except (http.client.HTTPException, OSError, urllib.error.URLError,
                ConnectionError, TimeoutError) as err:
            last_exc = err
            msg = str(err).lower()
            transient = any(k in msg for k in ("reset", "timeout", "timed out",
                                              "eof", "refused", "broken pipe"))
            if attempt < RETRY_ATTEMPTS and transient:
                time.sleep(RETRY_BACKOFF)
                continue
            raise
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass
    else:  # pragma: no cover - loop always breaks or raises
        raise RuntimeError(str(last_exc))

    plain = net.decrypt_response(raw, kd)
    try:
        outer = json.loads(plain.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise RuntimeError("无法解析业务响应: " + plain[:200].decode("utf-8", "replace"))

    # The gateway double-wraps: {"data": b64(businessJSON), "timeStamp": N}.
    inner_b64 = outer.get("data")
    if isinstance(inner_b64, str) and inner_b64:
        try:
            decoded = net._b64_decode_tolerant(inner_b64)
            biz = json.loads(decoded.decode("utf-8"))
            if isinstance(biz, dict):
                return biz
        except Exception:
            pass
    return outer if isinstance(outer, dict) else {}


def _str_field(obj: Any, key: str) -> str:
    """Pull a string field out of a decoded JSON object."""
    if not isinstance(obj, dict):
        return ""
    value = obj.get(key)
    return "" if value is None else str(value)


def _is_biz_success(env: Dict[str, Any]) -> bool:
    """True when the business envelope reports success.

    The gateway answers with code 1 for OK and 340000 for a business
    failure (4102 = no identity, 4103 = wrong identity form).
    """
    code = env.get("code")
    return code in (1, "1", 0, "0")


# --------------------------------------------------------------------------
# Identity
# --------------------------------------------------------------------------


def business_login(access_token: str) -> Optional[str]:
    """Exchange the Passport token for the short business uid.

    Returns None when the gateway refuses — the caller keeps using the
    passport token, which the business hosts also accept.
    """
    url = BIZ_HOST + "/user/user/login"
    try:
        env = _post_encrypted(url, [], access_token, "",
                               business_type=business_type_for(url))
    except Exception:
        return None
    data = env.get("data")
    if isinstance(data, dict):
        uid = data.get("uid") or data.get("Uid")
        if uid:
            return str(uid)
    return None


# --------------------------------------------------------------------------
# Vehicle endpoints
# --------------------------------------------------------------------------


# The `vehicle_type` codes the reference client sends per gateway.
# These are the values the official client uses; the gateway is lenient
# about them (small integers also work) but the travel endpoint filters
# on them, so use the real codes.
VEHICLE_TYPE_EBIKE = "65"
VEHICLE_TYPE_MOTOR = "64"
VEHICLE_TYPE_TRAVEL = "112"


# The `Business-Type` header is decided PER ENDPOINT, not per gateway.
# This table was read off the OFFICIAL CLI binary's live requests (point
# it at a local capture server and read the headers). The open-source
# reference implementation maps by host instead and therefore sends "1"
# to the travel gateway — which the gateway answers with an empty ride
# history (code=1 but times=0), so the omission is silent.
_BUSINESS_TYPE_BY_PATH = {
    "/vehicle/binding/my-vehicle": "1",
    "/vehicle/vehicle/desktop-component": "2",
    "/v6/vehicle/battery-info": "2",
    "/app-api/travel/v6/travel-list2": "2",
    "/app-api/travel/v6/travel-info": "2",
}


def business_type_for(url: str) -> str:
    """The `Business-Type` header each endpoint expects.

    Truth table comes from the official binary's captured headers; the
    host-based fallback below only matters for endpoints never observed.
    """
    path = urlparse(url).path if "://" in url else url
    if path in _BUSINESS_TYPE_BY_PATH:
        return _BUSINESS_TYPE_BY_PATH[path]
    if url.startswith(EBIKE_HOST):
        return "2"
    if url.startswith(BIZ_HOST):
        return "0"
    return "1"


# --------------------------------------------------------------------------
# Ride history
# --------------------------------------------------------------------------

# The travel gateway wants the tracking-library headers on top of the
# common set.
TRAVEL_HEADERS_EXTRA = {"rn-ver": "743", "rn-module": "Track", "rn-version": "0"}


def travel_list(access_token: str, uid: str, sn: str, month: str) -> Dict[str, Any]:
    """One month's ride summary for a vehicle.

    `month` is YYYYMM. The gateway answers code=1 with an empty payload
    when the Business-Type header is wrong, so the header is not
    optional here even though the response looks successful.
    """
    url = TRAVEL_HOST + "/app-api/travel/v6/travel-list2"
    extra: List[Tuple[str, Any]] = [
        ("wnumber", sn),
        ("rnVersion", "743"),
        ("vehicle_type", VEHICLE_TYPE_TRAVEL),
        ("month", month),
        ("page", "1"),
        ("current_version", APP_VERSION),
    ]
    headers = dict(TRAVEL_HEADERS_EXTRA)
    if access_token:
        headers["access_token"] = access_token
    headers["Accept"] = "application/json"
    return _post_encrypted(url, extra, access_token, uid,
                           business_type=business_type_for(url),
                           extra_headers=headers)


def travel_detail(access_token: str, uid: str, sn: str, travel_id: str) -> Dict[str, Any]:
    """One ride in full: timestamps, distance, energy and the GPS trail."""
    url = TRAVEL_HOST + "/app-api/travel/v6/travel-info"
    extra: List[Tuple[str, Any]] = [
        ("wnumber", sn),
        ("rnVersion", "743"),
        ("vehicle_type", VEHICLE_TYPE_TRAVEL),
        ("travel_id", travel_id),
        ("current_version", APP_VERSION),
    ]
    headers = dict(TRAVEL_HEADERS_EXTRA)
    if access_token:
        headers["access_token"] = access_token
    headers["Accept"] = "application/json"
    return _post_encrypted(url, extra, access_token, uid,
                           business_type=business_type_for(url),
                           extra_headers=headers)


def my_vehicle(access_token: str, uid: str = "") -> Dict[str, Any]:
    """The account's vehicle list.

    The reference client asks the ebike gateway AND the motor gateway with
    the same path and merges the two lists — either may come back empty.
    Fz-series e-bikes live on the ebike gateway; motor-class vehicles live
    on the motor one.

    The serial rides in `wnumber` (with `sn` as an accepted fallback), and
    `vehicle_type` selects the class.
    """
    merged: List[Dict[str, Any]] = []
    seen: set = set()
    last: Dict[str, Any] = {}
    ok = False
    for host, vtype in ((EBIKE_HOST, VEHICLE_TYPE_EBIKE),
                        (MOTOR_HOST, VEHICLE_TYPE_MOTOR)):
        url = host + "/vehicle/binding/my-vehicle"
        env = _post_encrypted(url, [("vehicle_type", vtype)],
                               access_token, uid,
                               business_type=business_type_for(url))
        last = env
        if not _is_biz_success(env):
            continue
        ok = True
        for item in env.get("data") or []:
            if not isinstance(item, dict):
                continue
            sn = _str_field(item, "wnumber") or _str_field(item, "sn")
            if sn and sn not in seen:
                seen.add(sn)
                merged.append(item)
    if not ok:
        return last
    return {"code": 1, "desc": "成功", "data": merged}


def desktop_component(access_token: str, uid: str, sn: str) -> Dict[str, Any]:
    """Vehicle status: location, power state, lock state, charge state.

    The serial rides in `sn_str` here — NOT `sn`.
    """
    url = EBIKE_HOST + "/vehicle/vehicle/desktop-component"
    return _post_encrypted(url, [("sn_str", sn)], access_token, uid)


def battery_info(access_token: str, uid: str, sn: str) -> Dict[str, Any]:
    """Battery detail: voltage, temperature, cycles, charge power.

    The serial rides in `wnumber` here. Both gateways answer; the ebike one
    is asked first because an Fz-series e-bike is an ebike-class vehicle
    (its `battery_type` is 2), and the motor gateway is the fallback.
    """
    last: Dict[str, Any] = {}
    for host in (EBIKE_HOST, MOTOR_HOST):
        url = host + "/v6/vehicle/battery-info"
        env = _post_encrypted(url, [("wnumber", sn)], access_token, uid,
                               business_type=business_type_for(url))
        last = env
        if env.get("code") in (0, 1, "0", "1"):
            return env
    return last


def travel_list(access_token: str, uid: str, sn: str, month: str = "") -> Dict[str, Any]:
    """Ride history for a month.

    The travel gateway is the one that wants extra headers: the token and
    the tracking-library ones. The month is mandatory — omitting it gets
    `参数错误` back.
    """
    url = TRAVEL_HOST + "/app-api/travel/v6/travel-list2"
    extra: List[Tuple[str, Any]] = [
        ("wnumber", sn),
        ("rnVersion", "743"),
        ("vehicle_type", VEHICLE_TYPE_TRAVEL),
        ("page", "1"),
        ("current_version", APP_VERSION),
    ]
    if month:
        extra.append(("month", month))
    return _post_encrypted(
        url, extra, access_token, uid, business_type=business_type_for(url),
        extra_headers={
            "access_token": access_token,
            "Accept": "application/json",
            "rn-ver": "743",
            "rn-module": "Track",
            "rn-version": "0",
        })


def whoami(access_token: str) -> Dict[str, Any]:
    """Verify the Passport token against the account endpoint."""
    url = BIZ_HOST + "/v5/user"
    return _post_encrypted(url, [], access_token, "")


__all__ = [
    "battery_info", "business_login", "desktop_component", "my_vehicle",
    "strip_proxy_env", "travel_list", "whoami",
]
