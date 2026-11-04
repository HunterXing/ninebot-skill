"""Ninebot (九号) NET ("Netease") encrypted business channel — pure Python.

This is a faithful port of the reference Go implementation in
`likz0421/ninebot-go` (MIT licensed), which is itself a documented
re-implementation of the official Ninebot app's wire protocol.

WHY THIS EXISTS (2026-11-04)
---------------------------
The account backend used to speak `POST /app-api/inner/device/ai/*` with a
Bearer passport token. Ninebot retired that route family in March 2026 —
verified live, it now answers HTTP 404 with
`requestId: oms-device-api-route-ai:...`.

What still works is the encrypted business channel the official app uses:

    POST ebike.ninebot.com/vehicle/binding/my-vehicle        (vehicle list)
    POST ebike.ninebot.com/vehicle/vehicle/desktop-component  (status)
    POST steeldust.ninebot.com/v6/vehicle/battery-info         (battery)

Those hosts answer HTTP 200 with `code 340000 / (4102)` when no identity is
attached, i.e. the route is alive and only the credential is missing.

`ninecli` (PyPI) implements this channel, but it ships ONLY prebuilt wheels —
no sdist, no source. Handing a password to an unauditable binary is not
acceptable for a credential that can change an account password, bind a phone
number, and reach the shop. So the crypto is ported here, in source we can
read, and the only third-party dependency is the system `openssl` binary for
AES (RSA and the key derivation are pure Python).

WIRE PROTOCOL
-------------
request  {"d": b64(AES(keyDataJSON)), "h": hex(md5(keyDataJSON)),
          "k": b64(RSA(kreq)), "p": "101", "t": "0"}
response {"v": 101, "s": b64(RSA sig), "r": b64(AES(respJSON))}

keyDataJSON embeds base64(inner) plus four random keyData strings, platform
and timeStamp. AES is 128-bit CBC with a zero IV and PKCS#7 padding. The
response key is DeriveKey(one, two, three, four).

The "k" field is encrypted with the NETEASE public key (a dedicated key
parsed from a separate embedded blob), NOT the Passport login key — using the
Passport key there yields server error 4102 ("通讯异常").

The response "s" signature is not verified: it does not validate under the
app's embedded Passport public key with PKCS#1 v1.5 over md5/sha1/sha256 of
either the raw or base64 "r" (0 of 184 real captures). The field is ignored,
exactly as the reference does.

ZERO THIRD-PARTY PYTHON DEPENDENCIES
------------------------------------
AES-128-CBC is delegated to the system `openssl enc` binary via subprocess
because this machine has no `cryptography` / `pycryptodome`. RSA, the key
derivation, MD5 and the envelope are implemented here in pure Python.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
import time
from typing import Any, Dict, List, Optional, Tuple

from ninebot_sign import _parse_rsa_public_key, rsa_encrypt_pkcs1v15

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

# RSA-1024 public key used for the envelope "k" field (AES key request).
# Source: reference implementation, recovered from the official app's
# embedded DER blob. NOT the Passport login key.
NETEASE_PUBLIC_KEY_PEM = """-----BEGIN PUBLIC KEY-----
MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDT3m0c/8y9c13PzaFbATEg+Zwd
kpPcCy0V21VKBBSx16ckVtLERAQ7EH8d6DqgEbyzayAwlQd1gDhUmx27hDWafXr9
/evUZkkBegcsNnKrIlh93lPKccjk+LDXS1TnDIIFiTlSbNnaYwehI/9pUbKCI3h7
yE0pum6hJh/9QtGPlwIDAQAB
-----END PUBLIC KEY-----"""

# Alphabet for keyDataFour (reference binary constant).
CHARSET_69 = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789;-_.,+/"

# Printable ASCII 0x21..0x7E, used for the 16-char session key.
CHARSET_94 = (
    "!\"#$%&'()*+,-./0123456789:;<=>?@ABCDEFGHIJKLMNOPQRSTUVWXYZ[\\]^_`"
    "abcdefghijklmnopqrstuvwxyz{|}~"
)

# Wrapper "platform" value; the reference sends 2 on the Android channel.
WRAPPER_PLATFORM = 2

OPENSSL_BIN = os.environ.get("OPENSSL_BIN", "openssl")


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------


def _b64(data: bytes) -> str:
    """Standard base64, no line breaks (android.util.Base64.NO_WRAP)."""
    return base64.b64encode(data).decode("ascii")


def _b64_decode_tolerant(text: str) -> bytes:
    """Decode base64, tolerating whitespace and URL-safe input."""
    cleaned = "".join(ch for ch in text if ch not in " \t\n\r")
    for decoder in (
        base64.b64decode,
        lambda s: base64.b64decode(s + "=" * (-len(s) % 4)),
        lambda s: base64.urlsafe_b64decode(s + "=" * (-len(s) % 4)),
    ):
        try:
            return decoder(cleaned)
        except Exception:
            continue
    raise ValueError("not valid base64")


def _md5_hex_lower(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def _md5_hex_upper(data: bytes) -> str:
    return hashlib.md5(data).hexdigest().upper()


# --------------------------------------------------------------------------
# AES-128-CBC via the system openssl binary
# --------------------------------------------------------------------------


def _openssl() -> str:
    path = shutil.which(OPENSSL_BIN)
    if not path:
        raise RuntimeError(
            "找不到 openssl（AES-128-CBC 依赖它）。"
            " 可用 OPENSSL_BIN=/path/to/openssl 指定路径。")
    return path


def aes_encrypt(plain: bytes, key: bytes) -> str:
    """AES-128-CBC, zero IV, PKCS#7 -> standard base64."""
    if len(key) not in (16, 24, 32):
        raise ValueError(f"aes: bad key len {len(key)}")
    # PKCS#7 pad by hand: openssl is invoked with -nopad.
    block = 16
    pad = block - len(plain) % block
    padded = plain + bytes([pad]) * pad
    proc = subprocess.run(
        [_openssl(), "enc", "-aes-%d-cbc" % (len(key) * 8), "-e", "-nopad",
         "-K", key.hex(), "-iv", ("00" * 16)],
        input=padded, capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError("openssl encrypt failed: " + proc.stderr.decode()[:200])
    return _b64(proc.stdout)


def aes_decrypt(b64_cipher: str, key: bytes) -> bytes:
    """AES-128-CBC, zero IV -> plaintext with PKCS#7 padding stripped."""
    if len(key) not in (16, 24, 32):
        raise ValueError(f"aes: bad key len {len(key)}")
    raw = _b64_decode_tolerant(b64_cipher)
    if not raw or len(raw) % 16 != 0:
        raise ValueError("aes: ciphertext not block-aligned")
    proc = subprocess.run(
        [_openssl(), "enc", "-aes-%d-cbc" % (len(key) * 8), "-d", "-nopad",
         "-K", key.hex(), "-iv", ("00" * 16)],
        input=raw, capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError("openssl decrypt failed: " + proc.stderr.decode()[:200])
    out = proc.stdout
    if not out:
        return b""
    pad = out[-1]
    if pad == 0 or pad > 16 or pad > len(out):
        raise ValueError("aes: invalid pkcs7 padding")
    if any(b != pad for b in out[-pad:]):
        raise ValueError("aes: corrupt pkcs7 padding")
    return out[:-pad]


# --------------------------------------------------------------------------
# Key material
# --------------------------------------------------------------------------


class KeyData:
    """One request's random key material."""

    __slots__ = ("kreq", "one", "two", "three", "four")

    def __init__(self, kreq: str, one: str, two: str, three: str, four: str):
        self.kreq = kreq
        self.one = one
        self.two = two
        self.three = three
        self.four = four


def _rand_hex(n: int) -> str:
    return os.urandom((n + 1) // 2).hex()[:n]


def _rand_from_charset(n: int, charset: str) -> str:
    # Unbiased rejection sampling over the charset.
    limit = (256 // len(charset)) * len(charset)
    out: List[str] = []
    while len(out) < n:
        for byte in os.urandom(n):
            if byte < limit:
                out.append(charset[byte % len(charset)])
                if len(out) == n:
                    break
    return "".join(out)


def gen_key_data() -> KeyData:
    """Fresh key material: 16-char session key + 11/11/10 hex + 8 chars."""
    return KeyData(
        kreq=_rand_from_charset(16, CHARSET_94),
        one=_rand_hex(11),
        two=_rand_hex(11),
        three=_rand_hex(10),
        four=_rand_from_charset(8, CHARSET_69),
    )


# --------------------------------------------------------------------------
# Key derivation (port of internal/crypto/keyderive.go)
# --------------------------------------------------------------------------


def _rotl32(value: int, n: int) -> int:
    n &= 31
    if n == 0:
        return value & 0xFFFFFFFF
    return ((value << n) | (value >> (32 - n))) & 0xFFFFFFFF


def _fold_string(text: str) -> int:
    acc = 0
    for byte in text.encode("utf-8"):
        acc = _rotl32(acc, 8) ^ byte
    return acc & 0xFFFFFFFF


def derive_key(one: str, two: str, three: str, four: str) -> bytes:
    """Fold the four keyData strings into a 16-byte AES-128 key."""
    w1 = _fold_string(one)
    w2 = _fold_string(two)
    w3 = _fold_string(three)
    w4 = _fold_string(four)

    v = w1 ^ w3
    tv = v ^ _rotl32(v, 8) ^ _rotl32(v, 24)
    a = w2 ^ tv
    b = w4 ^ tv

    u = w2 ^ w4
    tu = u ^ _rotl32(u, 8) ^ _rotl32(u, 24)
    c = w3 ^ tu
    d = w1 ^ tu

    p0 = (c & a) ^ d
    cb = (c | b) ^ a
    q = (c ^ b) ^ cb
    nq = (~q) & 0xFFFFFFFF
    p3 = p0 ^ nq
    p2 = (nq | p0) ^ cb
    p1 = ((~cb) & 0xFFFFFFFF & p3) ^ b

    return p1.to_bytes(4, "little") + p2.to_bytes(4, "little") + \
        p3.to_bytes(4, "little") + p0.to_bytes(4, "little")


# --------------------------------------------------------------------------
# Inner body + wrapper (port of internal/crypto/wrapper.go)
# --------------------------------------------------------------------------


def _json_escape(text: str) -> str:
    """Quote a string the way the reference client does.

    Mirrors the reference's `jsonEscapeString(s, escape=false)`: it always
    quotes, escapes the five JSON specials plus control characters, and
    leaves everything else — including non-ASCII — as-is. With
    `escape=false` the HTML-sensitive characters `<`, `>` and `&` are NOT
    escaped.
    """
    out = ['"']
    for ch in text:
        if ch == '"':
            out.append('\\"')
        elif ch == "\\":
            out.append("\\\\")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif ord(ch) < 0x20:
            out.append("\\u%04x" % ord(ch))
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _json_value(value: Any) -> str:
    """Render one ordered-JSON value: strings quoted, numbers bare."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if value is None:
        return "null"
    return _json_escape(str(value))


def build_inner(params: List[Tuple[str, Any]]) -> bytes:
    """Render the inner body: ordered JSON + serviceTime + nonce + checkcode.

    The reference implementation builds an *ordered* JSON object (insertion
    order preserved), appends `serviceTime` (unix millis) and `nonce`
    (32 hex chars), then computes the `checkcode` as the uppercase MD5 of
    that partial JSON — **without** the closing brace — and splices it in
    as the last member.

    An earlier revision emitted a flat `k=v&k=v` string instead, on the
    assumption that "the gateway accepts the flat form". That assumption
    was never verified, and it is wrong: the gateway answers
    `340000 … (4103)` for every business call. The gateway does parse the
    inner body as JSON — a flat form is not valid JSON, which is exactly
    what 4103 reports.
    """
    parts = []
    for key, value in params:
        if value is None:
            continue
        parts.append("%s:%s" % (_json_escape(key), _json_value(value)))
    parts.append('"serviceTime":%d' % int(time.time() * 1000))
    parts.append('"nonce":"%s"' % _rand_hex(32))
    partial = "{" + ",".join(parts)
    checkcode = _md5_hex_upper(partial.encode("utf-8"))
    return (partial + ',"checkcode":"' + checkcode + '"}').encode("utf-8")


def build_wrapper(inner: bytes, kd: KeyData, platform: int, timestamp: int) -> str:
    """Render the keyData JSON plaintext that gets encrypted into 'd'."""
    return (
        "{\n"
        "\t\"data\" : \"%s\",\n"
        "\t\"keyDataFour\" : \"%s\",\n"
        "\t\"keyDataOne\" : \"%s\",\n"
        "\t\"keyDataThree\" : \"%s\",\n"
        "\t\"keyDataTwo\" : \"%s\",\n"
        "\t\"platform\" : %d,\n"
        "\t\"timeStamp\" : %d\n"
        "}\n"
    ) % (
        _b64(inner), kd.four, kd.one, kd.three, kd.two, platform, timestamp,
    )


def encrypt_request(inner: bytes, kd: KeyData, platform: int = WRAPPER_PLATFORM,
                    timestamp: Optional[int] = None) -> Tuple[bytes, KeyData]:
    """Wrap the inner body into the encrypted envelope."""
    if timestamp is None:
        timestamp = int(time.time())
    key_data_json = build_wrapper(inner, kd, platform, timestamp).encode("utf-8")

    d = aes_encrypt(key_data_json, kd.kreq.encode("utf-8"))
    h = _md5_hex_lower(key_data_json)

    modulus, exponent = _parse_rsa_public_key(NETEASE_PUBLIC_KEY_PEM)
    k = _b64(rsa_encrypt_pkcs1v15(kd.kreq.encode("utf-8"), modulus, exponent))

    body = json.dumps({"d": d, "h": h, "k": k, "p": "101", "t": "0"},
                      separators=(",", ":")).encode("utf-8")
    return body, kd


def decrypt_response(body: bytes, kd: KeyData) -> bytes:
    """Unwrap an encrypted response using the request's keyData strings."""
    try:
        env = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return body
    if not isinstance(env, dict) or not env.get("r"):
        return body
    key = derive_key(kd.one, kd.two, kd.three, kd.four)
    return aes_decrypt(env["r"], key)


def compute_checkcode(ordered_json: bytes) -> str:
    """Uppercase hex MD5 — the inner request's 'checkcode' field."""
    return _md5_hex_upper(ordered_json)


__all__ = [
    "KeyData", "aes_decrypt", "aes_encrypt", "build_inner", "build_wrapper",
    "compute_checkcode", "derive_key", "decrypt_response", "encrypt_request",
    "gen_key_data",
]
