#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""End-to-end check: drive the real CLI against a fake Ninebot API.

Starts a local HTTP server that mimics the Ninebot gateway responses, points
the skill at it with an explicit --config, and asserts the CLI produces the
expected normalised JSON.

This proves the whole path (config merge -> request -> field mapping ->
normalisation -> exit code) without needing a real account or network.

Run:  python3 tests/e2e_fake_api.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
QUERY = os.path.join(ROOT, "scripts", "ninebot_query.py")

DEVICE_LIST = {
    "code": 0,
    "desc": "",
    "data": [
        {
            "sn": "N1DEC2315J0435",
            "deviceName": "小九",
            "img": "https://example.invalid/ignored.png",
            "extraAccountField": "must-not-be-echoed",
        }
    ],
}

DEVICE_INFO = {
    "code": 0,
    "msg": "",
    "data": {
        "dumpEnergy": 78,
        "powerStatus": "0",
        "locationInfo": {"locationDesc": "某市某区某路某号"},
        "estimateMileage": 42.6,
        "chargingState": "0",
        "remainChargeTime": "",
    },
}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):  # keep the CI log clean
        pass

    def _send(self, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if "get-device-list" in self.path:
            self._send(DEVICE_LIST)
        else:
            self._send({"code": 404, "msg": "no route"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        if "get-device-dynamic-info" in self.path:
            self._send(DEVICE_INFO)
        elif "get-device-list" in self.path:
            self._send(DEVICE_LIST)
        else:
            self._send({"code": 404, "msg": "no route"})


def main() -> int:
    server = HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    cfg = {
        "base_url": f"http://127.0.0.1:{port}",
        "auth": {"api_key_header": "Authorization", "api_key_prefix": "Bearer "},
        "devices": {"method": "GET", "path": "/ai-skill/api/device/info/get-device-list"},
        "device_info": {"method": "POST", "path": "/ai-skill/api/device/info/get-device-dynamic-info"},
    }

    with tempfile.TemporaryDirectory() as tmp:
        cfg_path = os.path.join(tmp, "config.json")
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump(cfg, f)

        env = dict(os.environ)
        env["NINEBOT_DEVICESERVICE_KEY"] = "test-key"
        # point the script at the fake gateway
        env["HTTPS_PROXY"] = ""
        env["HTTP_PROXY"] = ""

        print("=== single device (no selector) ===")
        proc = subprocess.run(
            [sys.executable, QUERY, "--backend", "official", "--config", cfg_path],
            capture_output=True, text=True, env=env, timeout=60,
        )
        print("exit:", proc.returncode)
        print("stdout:", proc.stdout.strip())
        if proc.returncode != 0:
            print("stderr:", proc.stderr[:500], file=sys.stderr)
            return 1
        data = json.loads(proc.stdout)
        assert data["battery"] == 78, data
        assert data["powerStatus"] == "OFF", data
        assert data["chargingState"] == "not_charge", data
        assert data["location"] == "某市某区某路某号", data
        assert data["estimateMileage"] == 42.6, data
        assert "extraAccountField" not in proc.stdout, "leaked upstream metadata"
        assert "ignored.png" not in proc.stdout, "leaked upstream metadata"

        print()
        print("=== explicit device name ===")
        proc2 = subprocess.run(
            [sys.executable, QUERY, "--backend", "official", "--config", cfg_path,
             "--device-name", "小九"],
            capture_output=True, text=True, env=env, timeout=60,
        )
        print("exit:", proc2.returncode)
        assert proc2.returncode == 0, proc2.stderr[:400]
        assert json.loads(proc2.stdout)["device_name"] == "小九"

        print()
        print("=== unknown device name -> exit 4 ===")
        proc3 = subprocess.run(
            [sys.executable, QUERY, "--backend", "official", "--config", cfg_path,
             "--device-name", "不存在的车"],
            capture_output=True, text=True, env=env, timeout=60,
        )
        print("exit:", proc3.returncode)
        assert proc3.returncode == 4, proc3.stdout
        out3 = json.loads(proc3.stdout)
        assert out3["error"] == "device not found", out3
        # the device list in the error must still be projected
        assert set(out3["devices"][0].keys()) == {"sn", "name"}, out3

    server.shutdown()
    print()
    print("E2E OK — full CLI path verified against a fake Ninebot endpoint")
    return 0


if __name__ == "__main__":
    sys.exit(main())
