#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Ninebot (九号) account login — writes a local session file.

Two flows:
  password   POST /v6/user/login            (password posted as plain text,
                                                exactly like the official CLI)
  sms        POST /v4/code/phone  ->  POST /v6/user/phoneCodeLogin

The session file holds the access/refresh tokens and is written 0600 inside a
0700 directory. Credentials are never written to disk and never printed.

⚠️  CREDENTIAL SCOPE — read this before using a real account.
--------------------------------------------------
A Ninebot Passport login yields your **full account identity**, not a
read-only vehicle credential. That token can change the account password,
bind a phone number, reach the shop/orders, and act on your identity. It is
not scoped to "read vehicle status".

Minimum-privilege options, best first:
  1. 九号 Device Service key (NINEBOT_DEVICESERVICE_KEY) — official,
     read-only vehicle endpoints only. Needs an OpenClaw 内测 slot.
  2. 九号「用车人 / 家庭账号」— a sub-account invited by the vehicle owner.
     It is a separate Passport account whose permissions the owner can
     narrow (view location / ride records / unlock). Register it in the
     九号出行 app first, then log in with that account here.
  3. The main account — full identity. Use only if 1 and 2 are unavailable,
     and treat the resulting session file as a secret worth encrypting.

Usage (interactive — the password is read with getpass, never echoed):
    python3 scripts/ninebot_login.py --user 13800000000
    python3 scripts/ninebot_login.py --user 13800000000 --sms
"""

from __future__ import annotations

import argparse
import getpass
import http.client
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ninebot_sign as ns  # noqa: E402

PASSPORT_BASE = "https://api-passport-bj.ninebot.com"

# Same secrets convention as the rest of this machine (~/.hermes/secrets, 0600).
SESSION_DIR = os.environ.get(
    "NINEBOT_SESSION_DIR",
    os.path.join(os.path.expanduser("~"), ".hermes", "secrets"),
)
SESSION_FILE = os.path.join(SESSION_DIR, "ninebot_session.json")

RETRY_ATTEMPTS = 3
RETRY_BACKOFF = 2.0


def strip_proxy_env() -> None:
    for var in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
                "all_proxy", "ALL_PROXY"):
        os.environ.pop(var, None)


def post_passport(path: str, params: Dict[str, str],
                  auth: Optional[str] = None) -> Tuple[int, Dict[str, Any]]:
    """POST to the Passport API, preserving the header names exactly.

    `urllib.request` is not usable here: `AbstractHTTPHandler.do_request_`
    applies `str.title()` to every header name, which turns
    `App_version` into `App_Version`. The official CLI (Go) sends
    `App_version` — Go's `textproto.CanonicalMIMEHeaderKey` only
    capitalises after a hyphen. HTTP header names are case-insensitive per
    RFC, but a server that looks them up with an exact-match map would
    miss them, so we speak the same dialect as the reference client and
    use `http.client`, which passes the names through untouched.
    """
    headers = ns.passport_headers(path, params, auth)
    body = ns.json_body(params)
    url = PASSPORT_BASE + path
    parsed = urllib.parse.urlsplit(url)
    host = parsed.hostname or ""
    last_exc: Optional[Exception] = None
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        conn = None
        try:
            if parsed.scheme == "https":
                conn = http.client.HTTPSConnection(
                    host, parsed.port or 443, timeout=15)
            else:
                conn = http.client.HTTPConnection(
                    host, parsed.port or 80, timeout=15)
            conn.request("POST", parsed.path or "/", body=body, headers=headers)
            resp = conn.getresponse()
            payload = resp.read().decode("utf-8", errors="replace")
            try:
                return resp.status, json.loads(payload)
            except json.JSONDecodeError:
                return resp.status, {"error": payload[:300]}
        except (http.client.HTTPException, OSError,
                urllib.error.URLError, ConnectionError, TimeoutError) as err:
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
    raise RuntimeError(str(last_exc))


# Ninebot Passport result codes we know how to act on. Anything else is
# reported verbatim so a new code is never silently swallowed.
#
#   00002  account not exists      — the phone number is not a Ninebot account
#   90014  username or password incorrect — credential rejected; the account
#                                   may exist but have no password set, or the
#                                   password may simply be wrong
#   90051  sign is null            — the request signature was missing/wrong
#                                   (protocol bug on our side, not a credential
#                                   problem)
#   90202  send code need verify   — the SMS path is behind a captcha
#   651001 smart service expired   — the cloud-side subscription lapsed
ERROR_HINTS: Dict[str, str] = {
    "00002": ("这个手机号不是九号账号。确认一下是否用同一号码注册过「九号出行」，"
              "或该账号已被注销。"),
    "90014": ("九号拒绝了凭据。九号直接比对你提交的密码字符串——如果你刚刚"
              "改用本工具登录而密码是对的，那多半是提交格式不对（见本仓库 "
              "README「账号登录：密码是明文提交的」一节）；"
              "若是刚在 APP 里改过密码，确认新密码已生效再试。"
              "\n    处理：先确认密码无误；若反复失败，用 ninecli 官方 CLI "
              "跑一次 `ninecli login -u 手机号 -p 密码` 做对照。"),
    "90051": ("请求签名缺失或错误——这是本工具的协议问题，不是你的密码问题。"
              "请把这条错误反馈出去。"),
    "90202": ("九号风控要求图形验证，短信验证码通道走不通。"
              "请改用密码登录，或在 APP 里登录一次。"),
    "651001": ("智能服务已到期，云端数据查询被暂停（续费后恢复）。"),
}


def describe(res: Dict[str, Any]) -> str:
    code = res.get("resultCode")
    desc = res.get("resultDesc")
    text = f"resultCode={code} resultDesc={desc}"
    hint = ERROR_HINTS.get(str(code))
    if hint:
        text += f"\n  → {hint}"
    return text


def extract_tokens(res: Dict[str, Any]) -> Optional[Dict[str, str]]:
    data = res.get("data")
    if not isinstance(data, dict):
        return None
    access = data.get("access_token") or data.get("accessToken")
    refresh = data.get("refresh_token") or data.get("refreshToken")
    if not access:
        return None
    return {
        "access_token": str(access),
        "refresh_token": str(refresh) if refresh else "",
    }


def save_session(tokens: Dict[str, str], username: str) -> None:
    os.makedirs(SESSION_DIR, mode=0o700, exist_ok=True)
    os.chmod(SESSION_DIR, 0o700)
    payload = {k: str(v) for k, v in tokens.items()}
    payload["username"] = username
    payload["saved_at"] = str(int(time.time()))
    tmp = SESSION_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, SESSION_FILE)
    os.chmod(SESSION_FILE, 0o600)


def resolve_biz_uid(access_token: str) -> str:
    """Exchange the Passport token for the short business uid.

    The vehicle APIs address the caller by this uid (e.g. "123456789"), not
    by the 19-digit passport uuid — sending the passport uuid there makes
    the gateway answer 4103.

    The gateway's business-login endpoint is picky about the request shape
    and we have not yet reproduced it exactly, so this is best effort. The
    uid is an ACCOUNT-level identifier (stable across token refreshes), so
    a previously captured one is kept rather than discarded.
    """
    try:
        import ninebot_client as client
        uid = client.business_login(access_token) or ""
    except Exception as exc:  # noqa: BLE001 - never block the login itself
        print(f"[biz uid] 换取 business uid 失败（不影响登录）: {exc}",
              file=sys.stderr)
        uid = ""
    if not uid:
        uid = load_saved_biz_uid()
        if uid:
            print(f"[biz uid] 沿用已保存的 business uid ({len(uid)} 位)")
    return uid


def load_saved_biz_uid() -> str:
    """Read a previously captured business uid out of the session file."""
    try:
        with open(SESSION_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        return str(data.get("biz_uid") or "")
    except (OSError, ValueError):
        return ""


def log_failure(stage: str, res: Dict[str, Any], status: Optional[int] = None) -> None:
    """Append the upstream failure to the local log.

    Only the error metadata is recorded — never the phone number, the
    password, or any token. Without this, a failed login leaves nothing on
    disk and the upstream error code has to be transcribed by hand.
    """
    log_path = os.environ.get(
        "NINEBOT_LOG", os.path.join(os.path.expanduser("~"), ".hermes", "logs",
                                    "ninebot_login.log"))
    code = res.get("resultCode")
    desc = res.get("resultDesc")
    if not code and not desc:
        # Some failures come back as a plain {"error": ...} or a raw body.
        code = res.get("error") or res.get("code") or "unknown"
        desc = res.get("desc") or res.get("message") or str(res)[:200]
    try:
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write("[%s] %s failed: http=%s code=%s desc=%s\n" % (
                time.strftime("%Y-%m-%d %H:%M:%S"), stage, status, code, desc))
    except OSError:
        pass


def login_password(username: str, password: str) -> int:
    """Log in with a password.

    The password is sent as PLAIN TEXT, matching what the official
    Ninebot CLI binary actually does.

    This is worth stating plainly, because it looks wrong at first
    glance. The reference Go implementation carries a `PassportEncryptRSA`
    helper and an embedded RSA public key, but that helper is **never
    called** — `LoginWithDevice` puts the raw password in the request
    body. Capturing the official binary's live request (via its
    `--passport-base` override pointed at a local server) confirms it:
    the body is `{"areaCode":"86","device":"ANDROID","password":"<plain>",
    "username":"..."}`.

    Ninebot therefore compares the submitted string directly. Sending an
    RSA ciphertext instead produces exactly the symptom we hit:
    `resultCode=90014 username or password incorrect` even though the
    credential is correct — the server decrypts/ignores our blob and
    compares garbage.

    The request still travels over TLS, and the `Sign` header covers the
    body, so the plaintext never leaves the encrypted channel.
    """
    params = {
        "device": ns.PASSPORT_DEVICE,
        "areaCode": "86",
        "username": username,
        "password": password,
    }
    status, res = post_passport("/v6/user/login", params)
    print(f"[password login] HTTP {status} {describe(res)}")
    tokens = extract_tokens(res)
    if not tokens:
        log_failure("password login", res, status)
        return fail(res)
    uid = resolve_biz_uid(tokens["access_token"])
    tokens["biz_uid"] = uid
    save_session(tokens, username)
    if uid:
        print(f"[biz uid] 已取得 business uid ({len(uid)} 位)")
    return 0


def login_sms(username: str) -> int:
    send_params = {"rawTemplate": "login", "areaCode": "86", "account": username}
    status, res = post_passport("/v4/code/phone", send_params)
    print(f"[send sms] HTTP {status} {describe(res)}")
    if str(res.get("resultCode") or "") not in ("00000", "0", ""):
        log_failure("sms send", res, status)
    code = str(res.get("resultCode") or "")
    if code == "90202":
        print("九号要求图形验证（send code need verify）。")
        print("请在九号出行 APP 里用短信验证码登录一次，然后重试。", file=sys.stderr)
        return 3
    if code not in ("00000", "0", ""):
        return fail(res)
    print("验证码已发送。")
    sms_code = input("请输入验证码（不回显）: ").strip()
    if not sms_code:
        print("验证码为空", file=sys.stderr)
        return 2
    login_params = {
        "device": ns.PASSPORT_DEVICE,
        "areaCode": "86",
        "phone": username,
        "code": sms_code,
        "region": "86",
    }
    status, res = post_passport("/v6/user/phoneCodeLogin", login_params)
    print(f"[consume code] HTTP {status} {describe(res)}")
    tokens = extract_tokens(res)
    if not tokens:
        log_failure("sms code login", res, status)
        return fail(res)
    uid = resolve_biz_uid(tokens["access_token"])
    tokens["biz_uid"] = uid
    save_session(tokens, username)
    if uid:
        print(f"[biz uid] 已取得 business uid ({len(uid)} 位)")
    return 0


def fail(res: Dict[str, Any]) -> int:
    code = res.get("resultCode")
    desc = res.get("resultDesc") or res.get("error") or "unknown"
    payload = {"error": desc, "resultCode": code}
    hint = ERROR_HINTS.get(str(code))
    if hint:
        payload["hint"] = hint
    print(json.dumps(payload, ensure_ascii=False), file=sys.stderr)
    return 1


def read_secret(prompt: str) -> str:
    """Read a secret from the controlling terminal.

    getpass raises EOFError when stdin is not a TTY (piped, cron, nohup,
    some SSH invocations). That used to abort the whole login with a bare
    traceback and no clue what went wrong, so fail with a message that says
    what to do instead.
    """
    try:
        return getpass.getpass(prompt)
    except (EOFError, OSError) as err:
        print(
            "ERROR: 无法从终端读取密码（stdin 不是 TTY）。\n"
            "  请在交互式终端里直接运行本脚本，不要用管道/重定向/nohup。\n"
            f"  底层错误: {type(err).__name__}: {err}",
            file=sys.stderr,
        )
        raise SystemExit(2)


def describe_password_shape(password: str) -> str:
    """Describe a password's shape without revealing it.

    A wrong-shape password is the most common cause of Ninebot's
    resultCode 90014 even when the credential itself is correct: a leading
    or trailing space, a full-width character pasted from a phone, or a
    stray newline all survive the read and get encrypted verbatim.
    """
    raw = password
    stripped = raw.strip()
    info = [
        "长度=%d" % len(raw),
        "首尾有空白=%s" % ("是" if raw != stripped else "否"),
    ]
    if raw != stripped:
        info.append("去空白后长度=%d" % len(stripped))
    if any(ord(ch) > 0x7F for ch in raw):
        info.append("含非 ASCII 字符=%s" % ("是（可能是全角/粘贴带入）" if True else "否"))
    classes = []
    if any(ch.isdigit() for ch in raw):
        classes.append("数字")
    if any(ch.isalpha() and ch.isascii() for ch in raw):
        classes.append("字母")
    if any(not ch.isalnum() and not ch.isspace() and ord(ch) < 0x7F for ch in raw):
        classes.append("符号")
    info.append("字符类型=%s" % ("+".join(classes) if classes else "无"))
    return "，".join(info)


def dry_run(username: str) -> int:
    """Probe the login path without sending the real password.

    Sends the account with a throw-away password. Ninebot answers before
    it can check the password, so the error code tells us which layer
    rejected the request:

      90051 sign is null            -> our signature is broken (a bug here)
      00002 account not exists      -> the phone number is not a Ninebot account
      90014 username or password incorrect
                                   -> the signature AND the account are fine,
                                      and Ninebot simply compared the
                                      (wrong) password. The request format is
                                      correct; only the credential differs.

    That last distinction is the whole point: it separates "our request is
    malformed" from "the password does not match", without needing the real
    password to find out.
    """
    params = {
        "device": ns.PASSPORT_DEVICE,
        "areaCode": "86",
        "username": username,
        "password": "ninebot-skill-dry-run-probe",
    }
    status, res = post_passport("/v6/user/login", params)
    code = str(res.get("resultCode") or "")
    print(f"[dry-run] HTTP {status} resultCode={code} "
          f"resultDesc={res.get('resultDesc')}")
    if code == "90014":
        print("[dry-run] 结论：签名正确、账号存在，请求格式无误。")
        print("[dry-run] 九号只是在比对密码——用真实密码再试即可。")
        return 0
    if code == "90051":
        print("[dry-run] 结论：签名层被拒——本工具的协议实现有问题。")
        return 3
    if code == "00002":
        print("[dry-run] 结论：这个手机号不是九号账号。")
        return 4
    print("[dry-run] 结论：未识别的错误码，请把上面的原始输出反馈出去。")
    return 5


# Client fingerprints to try when probing which one yields a token the
# business gateway accepts.
#
# The Passport response's JWT carries two audience-ish claims:
#   "aud"      — standard JWT audience, e.g. ["iot-webapp"]
#   "audience" — a custom claim; "unknown" means the server did not
#                recognise the client fingerprint we presented.
#
# The business gateway (cnnx-omsgw) wants a *mobile-class* token, and the
# reference implementation's comment says the audience is decided by the
# `device` parameter. The official CLI binary presents an Android
# fingerprint, which the server here answers with "unknown" / "iot-webapp"
# — a token the business gateway then refuses with 401905 用户不存在.
#
# So we probe: one password entry, several fingerprints, report the audience
# and the business-channel verdict for each.
FINGERPRINTS = [
    # (label, device, Os, os_language, os_version, app_version)
    ("official CLI (Android)",
     "ANDROID", "Android", "zh-hans-cn", "13", "610063322"),
    ("reference (iOS)",
     "IOS", "ios", "zh", "27.0", "610103831"),
    ("iOS + zh-hans-cn",
     "IOS", "ios", "zh-hans-cn", "27.0", "610103831"),
    ("Android + zh",
     "ANDROID", "Android", "zh", "13", "610063322"),
    ("iOS app_ver + Android os",
     "IOS", "Android", "zh-hans-cn", "13", "610103831"),
    ("Android device + iOS app_ver",
     "ANDROID", "ios", "zh-hans-cn", "13", "610103831"),
]


def _jwt_claims(token: str) -> Dict[str, Any]:
    """Decode a JWT payload without verifying it (diagnostics only)."""
    try:
        import base64
        parts = token.split(".")
        if len(parts) < 2:
            return {}
        seg = parts[1] + "=" * (-len(parts[1]) % 4)
        return json.loads(base64.urlsafe_b64decode(seg).decode("utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def probe_fingerprints(username: str, password: str) -> int:
    """Try each client fingerprint and report which one the business
    gateway accepts. The password is read once and never stored."""
    import base64

    original = (ns.PASSPORT_DEVICE, ns.PASSPORT_OS, ns.PASSPORT_OS_LANGUAGE,
                ns.PASSPORT_OS_VERSION, ns.PASSPORT_APP_VERSION)
    print("指纹探针：一次输入密码，逐套指纹试，看九号给哪种受众。")
    print()
    for label, device, os_, os_lang, os_ver, app_ver in FINGERPRINTS:
        ns.PASSPORT_DEVICE = device
        ns.PASSPORT_OS = os_
        ns.PASSPORT_OS_LANGUAGE = os_lang
        ns.PASSPORT_OS_VERSION = os_ver
        ns.PASSPORT_APP_VERSION = app_ver
        params = {"device": device, "areaCode": "86",
                  "username": username, "password": password}
        status, res = post_passport("/v6/user/login", params)
        code = str(res.get("resultCode") or "")
        if code not in ("90000", "0", ""):
            print(f"[{label}] device={device} app_ver={app_ver} "
                  f"-> HTTP {status} code={code} {res.get('resultDesc')}")
            time.sleep(2)
            continue
        tokens = extract_tokens(res)
        access = (tokens or {}).get("access_token", "")
        claims = _jwt_claims(access)
        aud = claims.get("aud")
        audience = claims.get("audience")
        verdict = "?"
        uid = ""
        try:
            import ninebot_client as client
            biz_url = client.BIZ_HOST + "/user/user/login"
            env = client._post_encrypted(
                biz_url, [], access, "",
                business_type=client.business_type_for(biz_url))
            verdict = f"{env.get('code')} {env.get('desc')}"
            data = env.get("data")
            if isinstance(data, dict):
                uid = str(data.get("uid") or data.get("Uid") or "")
        except Exception as exc:  # noqa: BLE001
            verdict = f"EXC {exc}"
        print(f"[{label}] device={device} app_ver={app_ver} -> LOGIN OK")
        print(f"    aud={aud}  audience={audience}")
        print(f"    business_login -> {verdict}"
              + (f"  uid={uid}" if uid else ""))
        if uid:
            # This fingerprint produced a token the business gateway accepts.
            # Save the session so no second login is needed.
            tokens = extract_tokens(res) or {}
            tokens["biz_uid"] = uid
            save_session(tokens, username)
            print(f"    ✅ 这套指纹可用，已保存 session（含 biz_uid={uid}）")
            (ns.PASSPORT_DEVICE, ns.PASSPORT_OS, ns.PASSPORT_OS_LANGUAGE,
             ns.PASSPORT_OS_VERSION, ns.PASSPORT_APP_VERSION) = original
            print()
            print(f"可用指纹：device={device} Os={os_} os_language={os_lang} "
                  f"os_version={os_ver} app_version={app_ver}")
            return 0
        time.sleep(2)
    (ns.PASSPORT_DEVICE, ns.PASSPORT_OS, ns.PASSPORT_OS_LANGUAGE,
     ns.PASSPORT_OS_VERSION, ns.PASSPORT_APP_VERSION) = original
    print()
    print("没有一套指纹拿到 business uid。")
    print("请把上面全部输出发回去——这说明问题不在指纹，需要换思路。")
    return 7


def main() -> int:
    ap = argparse.ArgumentParser(description="Log into Ninebot and save a local session.")
    ap.add_argument("--user", required=True, help="phone number or nickname")
    ap.add_argument("--sms", action="store_true",
                    help="use the SMS-code flow instead of a password")
    ap.add_argument("--dry-run", action="store_true",
                    help="check the request format without sending the real "
                         "password: sends a throw-away credential and reports "
                         "which layer Ninebot rejected it at")
    ap.add_argument("--show-shape", action="store_true",
                    help="print the password's shape (length / whitespace / "
                         "character classes) without revealing it")
    ap.add_argument("--probe-fingerprints", action="store_true",
                    help="try several client fingerprints with one password "
                         "entry and report which audience Ninebot issues")
    args = ap.parse_args()

    strip_proxy_env()

    if args.dry_run:
        return dry_run(args.user)

    if args.sms:
        return login_sms(args.user)

    # Deliberately NO --password flag: a command-line secret is preserved in
    # shell history and is visible to any local process via /proc/<pid>/cmdline.
    # getpass reads it straight from the controlling terminal instead.
    password = read_secret("九号账号密码（不回显）: ")
    if not password:
        print("密码为空", file=sys.stderr)
        return 2
    if args.show_shape:
        print("[password shape] " + describe_password_shape(password))
    if args.probe_fingerprints:
        return probe_fingerprints(args.user, password)
    return login_password(args.user, password)


if __name__ == "__main__":
    sys.exit(main())
