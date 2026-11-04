#!/usr/bin/env python3
"""Byte-for-byte comparison against the OFFICIAL Ninebot CLI binary.

This is the test that actually settled the login failure.

The official binary (`ninecli`, the Go executable shipped inside the PyPI
wheel) exposes `--passport-base`, so we can point it at a local fake
gateway and capture the exact request it posts to `/v6/user/login`.

We then replay the same request through our own implementation and compare
every header and the body byte for byte. The `Timestamp` is pinned to the
official one so the `Sign` headers are comparable.

Run standalone:  python3 tests/test_official_parity.py
It skips when the official binary is not installed.
"""

import http.server
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from typing import Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

PROBE_PASSWORD = "probe-password-123"
PROBE_PHONE = "13800000000"
SKIP_HEADERS = {"host", "content-length", "accept-encoding", "user-agent",
                "connection", "date", "server"}


def find_ninecli():
    for name in ("ninecli",):
        path = shutil.which(name)
        if path:
            return path
    guess = os.path.expanduser(
        "~/.local/share/uv/tools/ninecli/lib/python3.11/site-packages/"
        "ninecli/bin/ninecli")
    return guess if os.path.exists(guess) else None


class _Capture(http.server.BaseHTTPRequestHandler):
    captured = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length) if length else b""
        _Capture.captured.append({
            "path": self.path,
            "headers": dict(self.headers.items()),
            "body": body.decode("utf-8", errors="replace"),
        })
        payload = json.dumps({
            "resultCode": 90014,
            "resultDesc": "username or password incorrect"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _clean_env():
    env = dict(os.environ)
    for var in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
                "all_proxy", "ALL_PROXY"):
        env.pop(var, None)
    return env


class TestOfficialParity(unittest.TestCase):
    """Our Passport request must match the official CLI byte for byte."""

    official: Optional[dict]
    ours: Optional[dict]
    _restore: object

    @classmethod
    def setUpClass(cls):
        cls.ninecli = find_ninecli()
        if not cls.ninecli:
            raise unittest.SkipTest("official ninecli binary not installed")

        cls.port = _free_port()
        cls.srv = http.server.HTTPServer(("127.0.0.1", cls.port), _Capture)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{cls.port}"

        _Capture.captured = []
        with tempfile.TemporaryDirectory() as cfg:
            subprocess.run(
                [cls.ninecli, "login", "--config", cfg,
                 "--passport-base", base,
                 "-u", PROBE_PHONE, "-p", PROBE_PASSWORD],
                capture_output=True, text=True, env=_clean_env(), timeout=60)
        cls.official = _Capture.captured.pop() if _Capture.captured else None

        # Replay through our implementation with the official timestamp.
        import ninebot_sign as ns
        import ninebot_login as nl
        cls.ns = ns
        cls.nl = nl
        ts = (cls.official or {}).get("headers", {}).get("Timestamp")
        original = ns.passport_headers

        def pinned(path, params, auth=None):
            headers = original(path, params, auth)
            if ts:
                headers["Timestamp"] = ts
                headers["Sign"] = ns.sign(ns.canonical_string(path, params, ts))
            return headers

        ns.passport_headers = pinned
        nl.ns.passport_headers = pinned
        nl.PASSPORT_BASE = base
        cls._restore = original

        _Capture.captured = []
        nl.login_password(PROBE_PHONE, PROBE_PASSWORD)
        cls.ours = _Capture.captured.pop() if _Capture.captured else None

        cls.srv.shutdown()

    @classmethod
    def tearDownClass(cls):
        try:
            cls.ns.passport_headers = cls._restore
        except Exception:  # noqa: BLE001
            pass

    def test_official_request_was_captured(self):
        self.assertIsNotNone(self.official, "official binary sent no request")
        self.assertEqual(self.official["path"], "/v6/user/login")

    def test_our_request_was_captured(self):
        self.assertIsNotNone(self.ours, "our implementation sent no request")
        self.assertEqual(self.ours["path"], "/v6/user/login")

    def test_sign_matches(self):
        self.assertEqual(
            self.ours["headers"]["Sign"],
            self.official["headers"]["Sign"],
            "Sign differs — the canonical string or fingerprint drifted",
        )

    def test_body_matches(self):
        self.assertEqual(self.ours["body"], self.official["body"])

    def test_every_protocol_header_matches(self):
        for key in ("Clientid", "App_version", "Os", "Os_language",
                    "Os_version", "Timestamp", "Content-Type"):
            self.assertIn(key, self.official["headers"])
            self.assertEqual(
                self.ours["headers"].get(key),
                self.official["headers"][key],
                f"header {key} differs",
            )

    def test_no_extra_headers(self):
        """We must not send headers the official client does not."""
        official = {k.lower() for k in self.official["headers"]}
        ours = {k.lower() for k in self.ours["headers"]}
        self.assertEqual(ours - official, set(),
                         "we send headers the official client does not")

    def test_password_is_plaintext_in_both(self):
        """The official client posts the password as-is, not as a blob."""
        body = json.loads(self.official["body"])
        self.assertEqual(body["password"], PROBE_PASSWORD)
        ours = json.loads(self.ours["body"])
        self.assertEqual(ours["password"], PROBE_PASSWORD)

    def test_header_names_keep_their_case(self):
        """urllib's .title() would rewrite App_version into App_Version."""
        for key in ("App_version", "Os_language", "Os_version"):
            self.assertIn(key, self.ours["headers"],
                          f"header {key} lost its exact spelling")


if __name__ == "__main__":
    unittest.main(verbosity=2)
