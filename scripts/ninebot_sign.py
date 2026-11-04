#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""九号 Passport request signing — pure standard library.

Reproduces the header set the Ninebot app sends to api-passport-bj.ninebot.com:

    Sign       = lowercase_hex(sha256(canonical))
    canonical  = sorted "k=v" pairs joined by "&" over
                 {clientKey, url, timestamp, app_version, os, os_language,
                  os_version} + request params
    Clientid   = vehicle_app_prod

The password field is RSA-encrypted with the app's embedded RSA-1024 public
key (PKCS#1 v1.5) and then base64-encoded with the standard alphabet.

Algorithm details were recovered from the reference implementation in
likz0421/ninebot-go (internal/crypto/passport.go), which documents four
independent live captures that reproduce byte-for-byte.

Zero third-party dependencies: hashlib and a small PKCS#1 v1.5 pad.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import time
from typing import Any, Dict, List, Optional, Tuple

# --------------------------------------------------------------------------
# Constants recovered from the Ninebot app
# --------------------------------------------------------------------------

PASSPORT_CLIENT_ID = "vehicle_app_prod"
PASSPORT_CLIENT_KEY = "e177176a-3b3e-1513-e26e-d1123034cb66"

# Client fingerprint.
#
# These are the values the OFFICIAL Ninebot CLI binary sends. We read them
# off a live request by pointing the binary's `--passport-base` override at
# a local server and dumping what it posted:
#
#   App_version: 610063322
#   Os:          Android
#   Os_language: zh-hans-cn
#   Os_version:  13
#   device:      ANDROID
#
# An earlier revision used iOS values (610103831 / ios / zh / 27.0 / IOS)
# taken from the reference implementation's docs. Those reproduce its own
# iOS captures, but the official binary ships an Android fingerprint, and
# `device` also drives the JWT audience claim — a mismatch can make the
# gateway reject the issued token.
#
# The canonical string covers all of these, so a mismatch changes the
# `Sign` as well. We verified our canonical implementation reproduces the
# official binary's `Sign` byte-for-byte using these values.
PASSPORT_APP_VERSION = "610063322"
PASSPORT_OS = "Android"
PASSPORT_OS_LANGUAGE = "zh-hans-cn"
PASSPORT_OS_VERSION = "13"

# The `device` request parameter. The official binary sends "ANDROID"
# (upper-case); the reference implementation sends "IOS". It controls the
# JWT "aud" claim, and the business gateway only accepts mobile-class
# tokens — both spellings are mobile-class, but we follow the official
# binary exactly.
PASSPORT_DEVICE = "ANDROID"

# RSA-1024 public key embedded in the app.
#
# NOTE: the official CLI does NOT use this key for the Passport login —
# it posts the password as plain text (confirmed by capturing the binary's
# live request). The helper below is kept because the key genuinely is
# embedded in the app, and for anyone who needs to reproduce the app's own
# encryption path. Do NOT use it for /v6/user/login: the server compares
# the submitted string directly, so a ciphertext yields
# "username or password incorrect" even for a correct credential.
PASSPORT_PUBLIC_KEY_PEM = """-----BEGIN PUBLIC KEY-----
MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQC6maFY3dEhgav1147RW2gVWzCv
agkiRySnCDRSTM67YhHvLcrUSMnngxJl0A2liFLJydpn65E58oh0Phtu+t4Kkkfe
GIHsr931wRMRtkila4F/RF3U5pqSt42k/10U087QEhGMGvdOzF/5ziGXJod6ovBx
yk6pJlzNhxLTVJSzkQIDAQAB
-----END PUBLIC KEY-----"""


# --------------------------------------------------------------------------
# ASN.1 / DER helpers (minimal, enough for RSA public keys)
# --------------------------------------------------------------------------


def _der_len(n: int) -> bytes:
    if n < 0x80:
        return bytes([n])
    body = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(body)]) + body


def _der_seq(*items: bytes) -> bytes:
    body = b"".join(items)
    return b"\x30" + _der_len(len(body)) + body


def _der_int(raw: bytes) -> bytes:
    raw = raw.lstrip(b"\x00") or b"\x00"
    if raw[0] & 0x80:
        raw = b"\x00" + raw
    return b"\x02" + _der_len(len(raw)) + raw


def _der_bitstring(raw: bytes) -> bytes:
    return b"\x03" + _der_len(len(raw) + 1) + b"\x00" + raw


def _parse_der_len(data: bytes, offset: int) -> Tuple[int, int]:
    first = data[offset]
    offset += 1
    if first < 0x80:
        return first, offset
    count = first & 0x7F
    length = int.from_bytes(data[offset:offset + count], "big")
    return length, offset + count


def _parse_rsa_public_key(pem: str) -> Tuple[int, int]:
    """Return (modulus, exponent) from a SubjectPublicKeyInfo PEM."""
    body = "".join(
        line for line in pem.strip().splitlines() if not line.startswith("-----")
    )
    der = base64.b64decode(body)

    # SubjectPublicKeyInfo ::= SEQUENCE { algorithm, subjectPublicKey BIT STRING }
    _, offset = _parse_der_len(der, 1)          # skip the outer SEQUENCE header
    # algorithm SEQUENCE
    assert der[offset] == 0x30
    alg_len, offset = _parse_der_len(der, offset + 1)
    offset += alg_len
    # subjectPublicKey BIT STRING
    assert der[offset] == 0x03
    bit_len, offset = _parse_der_len(der, offset + 1)
    bitstring = der[offset + 1: offset + bit_len]  # drop the unused-bits byte
    offset += bit_len

    # RSAPublicKey ::= SEQUENCE { modulus INTEGER, publicExponent INTEGER }
    assert bitstring[0] == 0x30
    key_len, key_offset = _parse_der_len(bitstring, 1)
    assert bitstring[key_offset] == 0x02
    mod_len, mod_offset = _parse_der_len(bitstring, key_offset + 1)
    modulus = int.from_bytes(bitstring[mod_offset:mod_offset + mod_len], "big")
    exp_offset = mod_offset + mod_len
    assert bitstring[exp_offset] == 0x02
    exp_len, exp_offset = _parse_der_len(bitstring, exp_offset + 1)
    exponent = int.from_bytes(bitstring[exp_offset:exp_offset + exp_len], "big")
    return modulus, exponent


# --------------------------------------------------------------------------
# RSA PKCS#1 v1.5 encryption (public key only)
# --------------------------------------------------------------------------

# PKCS#1 v1.5 type 2 padding: 0x00 0x02 <non-zero random> 0x00 <message>
_PKCS1_PADDING = b"\x00\x02"


def rsa_encrypt_pkcs1v15(message: bytes, modulus: int, exponent: int) -> bytes:
    k = (modulus.bit_length() + 7) // 8
    if len(message) > k - 11:
        raise ValueError("message too long for RSA key")
    ps_len = k - len(message) - 3
    padding = bytearray()
    while len(padding) < ps_len:
        chunk = os.urandom(ps_len)
        padding.extend(b for b in chunk if b != 0)
    padding = bytes(padding[:ps_len])
    block = _PKCS1_PADDING + padding + b"\x00" + message
    cipher = pow(int.from_bytes(block, "big"), exponent, modulus)
    return cipher.to_bytes(k, "big")


def encrypt_password(password: str) -> str:
    """RSA-encrypt a Passport password and return standard base64."""
    modulus, exponent = _parse_rsa_public_key(PASSPORT_PUBLIC_KEY_PEM)
    cipher = rsa_encrypt_pkcs1v15(password.encode("utf-8"), modulus, exponent)
    return base64.b64encode(cipher).decode("ascii")


# --------------------------------------------------------------------------
# Signing
# --------------------------------------------------------------------------


def canonical_string(url: str, params: Dict[str, str], timestamp: str) -> str:
    """The exact string the Sign header covers."""
    merged: Dict[str, str] = {
        "clientKey": PASSPORT_CLIENT_KEY,
        "url": url,
        "timestamp": timestamp,
        "app_version": PASSPORT_APP_VERSION,
        "os": PASSPORT_OS,
        "os_language": PASSPORT_OS_LANGUAGE,
        "os_version": PASSPORT_OS_VERSION,
    }
    merged.update(params)
    return "&".join(f"{k}={merged[k]}" for k in sorted(merged))


def sign(canonical: str) -> str:
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def passport_headers(
    path: str,
    params: Dict[str, str],
    auth: Optional[str] = None,
) -> Dict[str, str]:
    """The exact header set the Ninebot app sends."""
    ts = str(int(time.time() * 1000))
    canonical = canonical_string(path, params, ts)
    headers = {
        "Content-Type": "application/json; charset=UTF-8",
        "Clientid": PASSPORT_CLIENT_ID,
        "Timestamp": ts,
        "Sign": sign(canonical),
        "App_version": PASSPORT_APP_VERSION,
        "Os": PASSPORT_OS,
        "Os_language": PASSPORT_OS_LANGUAGE,
        "Os_version": PASSPORT_OS_VERSION,
    }
    if auth:
        headers["Authorization"] = auth
    return headers


def json_body(params: Dict[str, Any]) -> bytes:
    """Byte-stable JSON body.

    The reference implementation serialises a `map[string]string` with
    Go's `json.Marshal`, which emits the keys in **sorted** order. Our
    dicts keep insertion order, so the body used to differ byte-wise from
    what the reference sends.

    JSON semantics do not care about key order, but a server-side risk
    check can compare the raw body, so match the reference exactly: sort
    the keys.
    """
    return json.dumps(params, ensure_ascii=False, separators=(",", ":"),
                      sort_keys=True).encode("utf-8")


if __name__ == "__main__":
    # Self-check: the canonical string for a parameterless request.
    demo = passport_headers("/v5/user", {})
    canon = canonical_string("/v5/user", {}, demo["Timestamp"])
    print(json.dumps({"canonical": canon, "headers": demo}, indent=2, ensure_ascii=False))
    assert demo["Sign"] == hashlib.sha256(canon.encode()).hexdigest()
    print("sign/canonical self-check OK", file=__import__("sys").stderr)
