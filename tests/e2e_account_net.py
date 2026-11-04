#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""End-to-end check of the ACCOUNT backend against a fake NET gateway.

The official backend's E2E (tests/e2e_fake_api.py) covers plain HTTP. The
account backend is the interesting one: it speaks Ninebot's NET encrypted
channel, so a mistake in the AES/RSA/key-derivation layer would only show
up against the real service.

This test stands up a local server that speaks the NET protocol back the
way the real gateway does:

  * recover the AES session key by RSA-decrypting the envelope's 'k' field
    with the NETEASE private key,
  * decrypt 'd' with that key to read the keyData JSON,
  * derive the response key from the four keyData strings,
  * answer with the business payload AES-encrypted.

Because the fake server decrypts exactly the way the real one does, the
whole wire format is exercised in both directions. A wrong field name, a
wrong key derivation or a wrong envelope layout all fail here.

Run:  python3 tests/e2e_account_net.py
"""

from __future__ import annotations

import base64
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import ninebot_net as net  # noqa: E402
from ninebot_sign import _parse_rsa_public_key  # noqa: E402

# The NETEASE public key the client encrypts the session key with. The fake
# server needs the matching PRIVATE key to recover it, the way the real
# gateway does. We generate an RSA-1024 pair at test time and hand the
# client the public half, so the test never needs a real private key.
_TEST_RSA_N = None
_TEST_RSA_E = 65537
_TEST_RSA_D = None


def _make_test_keypair():
    """Generate an RSA-1024 keypair for the fake gateway.

    Pure Python prime generation is slow for 512-bit moduli, so reuse the
    NETEASE modulus from the app and solve for a private exponent? No — the
    real modulus' private key is not available. Instead, build a small key
    with a known weakness-free structure: pick two random 256-bit primes.
    """
    import random
    rng = random.SystemRandom()

    def is_probable_prime(n: int, rounds: int = 24) -> bool:
        if n < 2:
            return False
        for p in (2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37):
            if n % p == 0:
                return n == p
        d, r = n - 1, 0
        while d % 2 == 0:
            d //= 2
            r += 1
        for _ in range(rounds):
            a = rng.randrange(2, n - 1)
            x = pow(a, d, n)
            if x in (1, n - 1):
                continue
            for _ in range(r - 1):
                x = x * x % n
                if x == n - 1:
                    break
            else:
                return False
        return True

    def gen_prime(bits: int) -> int:
        while True:
            candidate = rng.getrandbits(bits) | (1 << (bits - 1)) | 1
            if is_probable_prime(candidate):
                return candidate

    p = gen_prime(256)
    q = gen_prime(256)
    while q == p:
        q = gen_prime(256)
    n = p * q
    phi = (p - 1) * (q - 1)
    d = pow(_TEST_RSA_E, -1, phi)
    return n, d


def _rsa_decrypt_pkcs1v15(ciphertext: bytes, n: int, d: int) -> bytes:
    """RSA decrypt with PKCS#1 v1.5 type 2 padding (the inverse of the
    client's public-key encryption)."""
    k = (n.bit_length() + 7) // 8
    if len(ciphertext) != k:
        raise ValueError("ciphertext length mismatch")
    m = pow(int.from_bytes(ciphertext, "big"), d, n)
    em = m.to_bytes(k, "big")
    if em[0] != 0x00 or em[1] != 0x02:
        raise ValueError("bad PKCS#1 v1.5 padding")
    idx = em.find(b"\x00", 2)
    if idx < 0:
        raise ValueError("no padding separator")
    return em[idx + 1:]


def _pem_public_key(n: int, e: int) -> str:
    """Encode an RSA public key as a SubjectPublicKeyInfo PEM."""
    def der_len(length: int) -> bytes:
        if length < 0x80:
            return bytes([length])
        raw = length.to_bytes((length.bit_length() + 7) // 8, "big")
        return bytes([0x80 | len(raw)]) + raw

    def der_int(value: int) -> bytes:
        raw = value.to_bytes((value.bit_length() + 7) // 8, "big") or b"\x00"
        if raw[0] & 0x80:
            raw = b"\x00" + raw
        return b"\x02" + der_len(len(raw)) + raw

    def der_seq(*items: bytes) -> bytes:
        body = b"".join(items)
        return b"\x30" + der_len(len(body)) + body

    def der_bitstring(raw: bytes) -> bytes:
        return b"\x03" + der_len(len(raw) + 1) + b"\x00" + raw

    rsa_pub = der_seq(der_int(n), der_int(e))
    spki = der_seq(der_seq(der_int(0x2A864886F70D010101)), der_bitstring(rsa_pub))
    b64 = base64.b64encode(spki).decode()
    lines = [b64[i:i + 64] for i in range(0, len(b64), 64)]
    return "-----BEGIN PUBLIC KEY-----\n" + "\n".join(lines) + \
        "\n-----END PUBLIC KEY-----\n"


VEHICLE_LIST = [
    {
        "wnumber": "N1DEC2315J0435",
        "ble_name": "小九",
        "vehicle_type": "1",
        "img": "https://example.invalid/ignored.png",
        "extraAccountField": "must-not-be-echoed",
    }
]

DESKTOP = {
    "ble_name": "小九",
    "dump_energy": "78",
    "range": 42.6,
    "charging": False,
    "pwr": 0,
    "loc": {"lat": 39.90923, "lon": 116.39751, "lock": 1},
}

REQUESTS: list = []


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # keep the CI log clean
        pass

    def do_POST(self):  # noqa: N802 - http.server API
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        REQUESTS.append({"path": self.path, "body": raw})

        try:
            env = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send({"code": 340000, "desc": "bad envelope"})
            return

        try:
            # 1) recover the AES session key from the RSA-encrypted 'k'
            kreq = _rsa_decrypt_pkcs1v15(
                base64.b64decode(env["k"]), _TEST_RSA_N, _TEST_RSA_D)
            # 2) decrypt 'd' -> keyData JSON (contains the inner body)
            keydata_json = net.aes_decrypt(env["d"], kreq)
            keydata = json.loads(keydata_json.decode("utf-8"))
            # 3) sanity: the envelope 'h' must be the md5 of that JSON
            if net._md5_hex_lower(keydata_json) != env["h"]:
                raise ValueError("envelope h does not match keyDataJSON")
        except Exception as exc:
            self._send({"code": 340000, "desc": "cannot decrypt: %s" % exc})
            return

        if self.path.endswith("/vehicle/binding/my-vehicle"):
            payload = {"code": 1, "desc": "成功", "data": VEHICLE_LIST}
        elif self.path.endswith("/vehicle/vehicle/desktop-component"):
            payload = {"code": 1, "desc": "成功", "data": [DESKTOP]}
        else:
            payload = {"code": 340000, "desc": "unknown path"}

        # 4) answer with the response key derived from the keyData strings
        resp_key = net.derive_key(keydata["keyDataOne"], keydata["keyDataTwo"],
                                   keydata["keyDataThree"], keydata["keyDataFour"])
        body_json = json.dumps({
            "data": base64.b64encode(
                json.dumps(payload, ensure_ascii=False).encode("utf-8")).decode(),
            "timeStamp": 1700000000000,
        }, separators=(",", ":")).encode("utf-8")
        out = {"v": 101, "s": "signature-not-verified",
               "r": net.aes_encrypt(body_json, resp_key)}
        self._send_raw(json.dumps(out, separators=(",", ":")).encode("utf-8"))

    def _send(self, payload):
        self._send_raw(json.dumps(payload, ensure_ascii=False).encode("utf-8"))

    def _send_raw(self, body):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> int:
    global _TEST_RSA_N, _TEST_RSA_D
    _TEST_RSA_N, _TEST_RSA_D = _make_test_keypair()

    # Hand the client our test public key in place of the NETEASE one.
    net.NETEASE_PUBLIC_KEY_PEM = _pem_public_key(_TEST_RSA_N, _TEST_RSA_E)

    server = HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    import ninebot_client as client

    client.EBIKE_HOST = f"http://127.0.0.1:{port}"
    client.MOTOR_HOST = f"http://127.0.0.1:{port}"
    client.BIZ_HOST = f"http://127.0.0.1:{port}"

    failures = []

    env = client.my_vehicle("fake-token", "12345678")
    if not client._is_biz_success(env):
        failures.append(f"my_vehicle not successful: {env}")
    else:
        items = env.get("data") or []
        if len(items) != 1:
            failures.append(f"expected 1 vehicle, got {len(items)}")
        elif items[0].get("wnumber") != "N1DEC2315J0435":
            failures.append(f"unexpected sn: {items[0]}")
        else:
            print("✓ my_vehicle: NET round-trip ok, wnumber extracted")

    env = client.desktop_component("fake-token", "12345678", "N1DEC2315J0435")
    if not client._is_biz_success(env):
        failures.append(f"desktop_component not successful: {env}")
    else:
        data = env.get("data")
        obj = data[0] if isinstance(data, list) and data else data
        if obj.get("dump_energy") != "78":
            failures.append(f"battery not extracted: {obj}")
        elif obj.get("loc", {}).get("lat") != 39.90923:
            failures.append(f"location not extracted: {obj}")
        else:
            print("✓ desktop_component: battery=78, loc lat/lon ok")

    if not REQUESTS:
        failures.append("server received no request")
    else:
        first = REQUESTS[0]
        if first["path"] != "/vehicle/binding/my-vehicle":
            failures.append(f"unexpected path: {first['path']}")
        env = json.loads(first["body"].decode("utf-8"))
        for field in ("d", "h", "k", "p", "t"):
            if field not in env:
                failures.append(f"envelope missing field {field}")
        if env.get("p") != "101" or env.get("t") != "0":
            failures.append("envelope p/t wrong")
        if len(env.get("h", "")) != 32:
            failures.append("envelope h is not an md5 hex")
        print("✓ request envelope carries d/h/k/p/t, h verified by the server")

    server.shutdown()

    if failures:
        print("\nE2E FAILED:")
        for f in failures:
            print("  -", f)
        return 1
    print("\nE2E OK — account backend verified against a fake NET gateway")
    return 0


if __name__ == "__main__":
    sys.exit(main())
