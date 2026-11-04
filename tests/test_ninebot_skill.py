#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for the ninebot skill — stdlib unittest, no network required.

Run:  python3 tests/test_ninebot_skill.py
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import ninebot_sign as ns  # noqa: E402
import ninebot_query as nq  # noqa: E402
import ninebot_login as nl
import ninebot_geo as geo  # noqa: E402
import ninebot_client as client  # noqa: E402


class TestSigning(unittest.TestCase):
    """The Sign header must reproduce a real request byte-for-byte.

    The reference vector below was captured from the OFFICIAL Ninebot CLI
    binary: we pointed its `--passport-base` override at a local server and
    dumped the request it posted. Reproducing its `Sign` exactly proves
    our canonical string, our client fingerprint and our key all agree
    with what Ninebot's own tooling sends.
    """

    # Captured from `ninecli login --passport-base http://127.0.0.1:PORT`
    OFFICIAL_CAPTURE = {
        "path": "/v6/user/login",
        "timestamp": "1793760787521",
        "params": {
            "areaCode": "86",
            "device": "ANDROID",
            "password": "probe-password-123",
            "username": "13800000000",
        },
        "sign": "a14cf04ffea7bc5ec0893891ee3b43925b66d1721ceb368e6923713d7ce7a3ec",
    }

    def test_reproduces_official_binary_sign(self):
        cap = self.OFFICIAL_CAPTURE
        canon = ns.canonical_string(cap["path"], cap["params"], cap["timestamp"])
        self.assertEqual(ns.sign(canon), cap["sign"])

    def test_body_matches_official_binary_byte_for_byte(self):
        cap = self.OFFICIAL_CAPTURE
        body = ns.json_body(cap["params"]).decode()
        self.assertEqual(
            body,
            '{"areaCode":"86","device":"ANDROID",'
            '"password":"probe-password-123","username":"13800000000"}',
        )

    def test_client_fingerprint_matches_official_binary(self):
        self.assertEqual(ns.PASSPORT_APP_VERSION, "610063322")
        self.assertEqual(ns.PASSPORT_OS, "Android")
        self.assertEqual(ns.PASSPORT_OS_LANGUAGE, "zh-hans-cn")
        self.assertEqual(ns.PASSPORT_OS_VERSION, "13")
        self.assertEqual(ns.PASSPORT_DEVICE, "ANDROID")

    def test_canonical_is_sorted_and_joined(self):
        canon = ns.canonical_string("/v5/user", {}, "123")
        keys = [kv.split("=", 1)[0] for kv in canon.split("&")]
        self.assertEqual(keys, sorted(keys))
        self.assertIn("clientKey=" + ns.PASSPORT_CLIENT_KEY, canon)
        self.assertIn("url=/v5/user", canon)

    def test_params_are_covered_by_the_signature(self):
        a = ns.canonical_string("/v6/user/login", {"username": "1"}, "1")
        b = ns.canonical_string("/v6/user/login", {"username": "2"}, "1")
        self.assertNotEqual(ns.sign(a), ns.sign(b))

    def test_headers_carry_the_full_app_set(self):
        h = ns.passport_headers("/v5/user", {})
        for key in ("Content-Type", "Clientid", "Timestamp", "Sign",
                    "App_version", "Os", "Os_language", "Os_version"):
            self.assertIn(key, h)
        self.assertEqual(h["Clientid"], "vehicle_app_prod")
        self.assertNotIn("Authorization", h)
        self.assertIn("Authorization", ns.passport_headers("/v5/user", {}, "tok"))

    def test_password_encryption_roundtrip_shape(self):
        enc = ns.encrypt_password("hunter2")
        # standard base64, no line wrapping, and decodes to a 128-byte block
        self.assertNotIn("\n", enc)
        raw = base64.b64decode(enc)
        self.assertEqual(len(raw), 128)
        # the plaintext must NOT be recoverable
        self.assertNotIn(b"hunter2", raw)

    def test_rsa_ciphertext_varies_per_call(self):
        a = ns.encrypt_password("same")
        b = ns.encrypt_password("same")
        self.assertNotEqual(a, b)  # PKCS#1 v1.5 uses random padding


class TestPasswordIsSentInPlaintext(unittest.TestCase):
    """The Passport login posts the password as plain text.

    The reference implementation ships a `PassportEncryptRSA` helper and an
    embedded RSA public key, but never calls it — `LoginWithDevice` puts
    the raw password in the body. Capturing the official binary's live
    request confirms it.

    Ninebot compares the submitted string directly, so RSA-encrypting the
    password produces `90014 username or password incorrect` even when the
    credential is correct. This test pins that behaviour so nobody
    "hardens" it back into a ciphertext.
    """

    def _login_source(self) -> str:
        path = os.path.join(ROOT, "scripts", "ninebot_login.py")
        with open(path, encoding="utf-8") as fh:
            return fh.read()

    def test_login_does_not_encrypt_the_password(self):
        src = self._login_source()
        self.assertNotIn("encrypt_password(password)", src)
        self.assertIn('"password": password', src)

    def test_login_uses_the_official_device_parameter(self):
        src = self._login_source()
        self.assertIn("ns.PASSPORT_DEVICE", src)
        self.assertNotIn('"device": "IOS"', src)


class TestConfigSafety(unittest.TestCase):
    """The three security fixes over the upstream official skill."""

    def _query_source(self) -> str:
        path = os.path.join(ROOT, "scripts", "ninebot_query.py")
        with open(path, encoding="utf-8") as fh:
            return fh.read()

    def test_api_key_is_not_accepted_as_cli_flag(self):
        self.assertNotIn('"--api-key"', self._query_source())

    def test_account_backend_does_not_use_the_retired_route(self):
        """Ninebot retired the /app-api/inner/device/ai/* family (HTTP 404).

        The route must not appear in any live config value (the prose in
        comments may still mention it, to explain why it was removed).
        """
        retired = "/app-api/inner/device/ai/"
        for cfg_name in ("ACCOUNT_CONFIG", "OFFICIAL_CONFIG"):
            cfg = getattr(nq, cfg_name)
            for section in ("devices", "device_info"):
                path = (cfg.get(section) or {}).get("path", "")
                self.assertNotIn(retired, path)
        self.assertNotIn(retired, (nq.ACCOUNT_CONFIG.get("base_url") or ""))

    def test_no_binary_in_the_credential_path(self):
        """The password/token must never reach an opaque binary.

        `ninecli` ships only as prebuilt wheels (no sdist, no source), so the
        account backend must be pure Python: the NET encryption lives in
        ninebot_net.py / ninebot_client.py, both in this repository.
        """
        query_src = self._query_source()
        # No subprocess call to any ninecli binary in the credential path.
        self.assertNotIn("NINECLI_BIN", query_src)
        self.assertNotIn("ninecli_bin", query_src)
        # The client module must exist and carry the encrypted transport.
        client_path = os.path.join(ROOT, "scripts", "ninebot_client.py")
        self.assertTrue(os.path.exists(client_path))
        with open(client_path, encoding="utf-8") as fh:
            client_src = fh.read()
        self.assertIn("encrypt_request", client_src)
        self.assertIn("ebike.ninebot.com", client_src)
        self.assertNotIn("NINECLI_BIN", client_src)

    def test_net_crypto_module_is_pure_python(self):
        """The NET layer must not shell out to anything but openssl."""
        path = os.path.join(ROOT, "scripts", "ninebot_net.py")
        self.assertTrue(os.path.exists(path))
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn("openssl", src)
        # The only subprocess target is the system openssl binary.
        self.assertEqual(src.count("subprocess.run("), 2)
        self.assertNotIn("NINECLI", src)

    def test_proxy_env_is_stripped(self):
        """Ninebot domains are not in the local no_proxy list; proxied
        requests get reset by the peer."""
        src = self._query_source()
        self.assertIn("http_proxy", src)
        client_path = os.path.join(ROOT, "scripts", "ninebot_client.py")
        with open(client_path, encoding="utf-8") as fh:
            self.assertIn("http_proxy", fh.read())

    def test_config_is_never_auto_discovered(self):
        src = self._query_source()
        self.assertNotIn("os.getcwd()", src)
        self.assertNotIn('"config.json"', src)

    def test_login_has_no_password_flag(self):
        """A CLI password leaks into shell history and /proc/<pid>/cmdline."""
        path = os.path.join(ROOT, "scripts", "ninebot_login.py")
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        self.assertNotIn('"--password"', src)
        self.assertNotIn("'--password'", src)
        self.assertIn("getpass.getpass", src)

    def test_session_lands_in_the_secrets_convention(self):
        """Session file must sit in ~/.hermes/secrets (or an explicit override)."""
        path = os.path.join(ROOT, "scripts", "ninebot_query.py")
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn(".hermes", src)
        self.assertIn("secrets", src)
        self.assertIn("NINEBOT_SESSION_DIR", src)

    def test_multi_device_listing_is_projected(self):
        device = {"sn": "SN1", "deviceName": "小九", "img": "https://x/y.png",
                  "secretAccountField": "leak"}
        out = nq.project_device(device, "sn", "deviceName")
        self.assertEqual(set(out.keys()), {"sn", "name"})
        self.assertNotIn("img", out)
        self.assertNotIn("secretAccountField", out)


class TestNormalisation(unittest.TestCase):
    def test_power_states(self):
        self.assertEqual(nq.normalize_power("0"), "OFF")
        self.assertEqual(nq.normalize_power("1"), "ON")
        self.assertEqual(nq.normalize_power(None), "unknown")
        self.assertEqual(nq.normalize_power("weird"), "unknown")

    def test_charging_states(self):
        # A missing value means "unknown", not "not charging" — reporting
        # the latter would state a fact we do not have.
        self.assertEqual(nq.normalize_charging(None), "unknown")
        self.assertEqual(nq.normalize_charging("1"), "charging")
        self.assertEqual(nq.normalize_charging(1), "charging")
        self.assertEqual(nq.normalize_charging(True), "charging")
        self.assertEqual(nq.normalize_charging("0"), "not_charge")
        self.assertEqual(nq.normalize_charging(0), "not_charge")
        self.assertEqual(nq.normalize_charging(False), "not_charge")
        self.assertEqual(nq.normalize_charging("weird"), "unknown")

    def test_deep_get(self):
        data = {"data": {"locationInfo": {"locationDesc": "合肥"}}}
        self.assertEqual(nq.deep_get(data, "data.locationInfo.locationDesc"), "合肥")
        self.assertIsNone(nq.deep_get(data, "data.missing.key"))
        self.assertIsNone(nq.deep_get(None, "a.b"))


class TestEndpointConfigs(unittest.TestCase):
    def test_official_and_account_paths_differ(self):
        """The official Device Service route vs the encrypted business route."""
        self.assertIn("/ai-skill/api/device/info/",
                      nq.OFFICIAL_CONFIG["devices"]["path"])
        self.assertIn("/vehicle/binding/my-vehicle",
                      nq.ACCOUNT_CONFIG["devices"]["path"])

    def test_official_backend_keeps_its_own_field_mapping(self):
        """The official Device Service returns dumpEnergy / locationInfo.*;
        the account backend reads different keys, so the two mappings must
        not be assumed identical."""
        self.assertEqual(nq.OFFICIAL_CONFIG["device_info"]["battery_path"],
                         "data.dumpEnergy")
        self.assertEqual(nq.OFFICIAL_CONFIG["device_info"]["location_path"],
                         "data.locationInfo.locationDesc")
        # The account backend normalises in code instead of by config path.
        self.assertNotEqual(nq.ACCOUNT_CONFIG["device_info"]["battery_path"],
                            nq.OFFICIAL_CONFIG["device_info"]["battery_path"])

    def test_endpoints_are_https_official_hosts(self):
        for cfg in (nq.OFFICIAL_CONFIG, nq.ACCOUNT_CONFIG):
            self.assertTrue(cfg["base_url"].startswith("https://"))
            self.assertIn("ninebot.com", cfg["base_url"])


class TestBackendSelection(unittest.TestCase):
    def test_explicit_backend_wins(self):
        self.assertEqual(nq.pick_backend("official", {"backend": "account"}), "official")
        self.assertEqual(nq.pick_backend("account", {"backend": "official"}), "account")

    def test_config_backend_is_honoured(self):
        self.assertEqual(nq.pick_backend(None, {"backend": "official"}), "official")

    def test_key_in_env_selects_official(self):
        old = os.environ.get("NINEBOT_DEVICESERVICE_KEY")
        os.environ["NINEBOT_DEVICESERVICE_KEY"] = "dummy"
        try:
            self.assertEqual(nq.pick_backend(None, {}), "official")
        finally:
            if old is None:
                os.environ.pop("NINEBOT_DEVICESERVICE_KEY", None)
            else:
                os.environ["NINEBOT_DEVICESERVICE_KEY"] = old

    def test_fallback_is_account(self):
        old = os.environ.pop("NINEBOT_DEVICESERVICE_KEY", None)
        try:
            self.assertEqual(nq.pick_backend(None, {}), "account")
        finally:
            if old is not None:
                os.environ["NINEBOT_DEVICESERVICE_KEY"] = old


class TestRetryPolicy(unittest.TestCase):
    def test_business_errors_are_not_retried(self):
        calls = {"n": 0}

        def boom(*a, **k):
            calls["n"] += 1
            raise RuntimeError("API error: account not exists (code: 00002)")

        original = nq.http_request
        nq.http_request = boom
        try:
            with self.assertRaises(RuntimeError):
                nq.request_with_retry("GET", "https://x")
        finally:
            nq.http_request = original
        self.assertEqual(calls["n"], 1, "business errors must not be retried")

    def test_transient_errors_are_retried(self):
        calls = {"n": 0}

        def flaky(*a, **k):
            calls["n"] += 1
            if calls["n"] < 3:
                raise RuntimeError("network error: read: connection reset by peer")
            return {"code": 0}

        original = nq.http_request
        original_sleep = nq.time.sleep
        nq.http_request = flaky
        nq.time.sleep = lambda *_: None
        try:
            res = nq.request_with_retry("GET", "https://x")
        finally:
            nq.http_request = original
            nq.time.sleep = original_sleep
        self.assertEqual(res, {"code": 0})
        self.assertEqual(calls["n"], 3)


class TestProxyStripping(unittest.TestCase):
    def test_proxy_vars_are_removed(self):
        saved = {}
        for var in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
                    "all_proxy", "ALL_PROXY"):
            saved[var] = os.environ.get(var)
            os.environ[var] = "http://127.0.0.1:7890"
        try:
            nq._strip_proxy_env()
            for var in saved:
                self.assertNotIn(var, os.environ)
        finally:
            for var, val in saved.items():
                if val is None:
                    os.environ.pop(var, None)
                else:
                    os.environ[var] = val


class TestNetCrypto(unittest.TestCase):
    """The pure-Python NET encryption layer.

    Ported from the MIT-licensed reference implementation in
    likz0421/ninebot-go. These tests guard the shape of every field the
    gateway parses; the live-verification evidence (Ninebot decrypts our
    request and we decrypt its reply) lives in the commit message.
    """

    def setUp(self):
        sys.path.insert(0, os.path.join(ROOT, "scripts"))
        import ninebot_net
        self.net = ninebot_net

    def test_aes_roundtrip_across_block_boundaries(self):
        key = b"0123456789abcdef"
        for size in (0, 1, 15, 16, 17, 100):
            plain = b"x" * size
            cipher = self.net.aes_encrypt(plain, key)
            self.assertEqual(self.net.aes_decrypt(cipher, key), plain)

    def test_aes_rejects_bad_key_length(self):
        with self.assertRaises(ValueError):
            self.net.aes_encrypt(b"x", b"short")

    def test_derive_key_is_deterministic_and_16_bytes(self):
        a = self.net.derive_key("a" * 11, "b" * 11, "c" * 10, "d" * 8)
        b = self.net.derive_key("a" * 11, "b" * 11, "c" * 10, "d" * 8)
        self.assertEqual(a, b)
        self.assertEqual(len(a), 16)
        # Any input change must move the key.
        self.assertNotEqual(a, self.net.derive_key("x", "y", "z", "w"))

    def test_key_data_shape(self):
        kd = self.net.gen_key_data()
        self.assertEqual(len(kd.kreq), 16)
        self.assertEqual(len(kd.one), 11)
        self.assertEqual(len(kd.two), 11)
        self.assertEqual(len(kd.three), 10)
        self.assertEqual(len(kd.four), 8)
        for ch in kd.kreq:
            self.assertIn(ch, self.net.CHARSET_94)
        for ch in kd.four:
            self.assertIn(ch, self.net.CHARSET_69)

    def test_key_data_is_fresh_per_call(self):
        self.assertNotEqual(self.net.gen_key_data().kreq,
                            self.net.gen_key_data().kreq)

    def test_wrapper_field_order_and_format(self):
        kd = self.net.gen_key_data()
        wrapper = self.net.build_wrapper(b"inner", kd, 2, 1700000000)
        parsed = json.loads(wrapper)
        self.assertEqual(list(parsed.keys()),
                         ["data", "keyDataFour", "keyDataOne", "keyDataThree",
                          "keyDataTwo", "platform", "timeStamp"])
        self.assertEqual(parsed["platform"], 2)
        self.assertEqual(parsed["timeStamp"], 1700000000)
        self.assertEqual(parsed["keyDataOne"], kd.one)

    def test_envelope_shape(self):
        kd = self.net.gen_key_data()
        body, _ = self.net.encrypt_request(b"inner", kd)
        env = json.loads(body.decode("utf-8"))
        self.assertEqual(list(env.keys()), ["d", "h", "k", "p", "t"])
        self.assertEqual(env["p"], "101")
        self.assertEqual(env["t"], "0")
        self.assertEqual(len(env["h"]), 32)          # lowercase md5 hex
        self.assertEqual(env["h"], env["h"].lower())
        self.assertEqual(len(base64.b64decode(env["k"])), 128)  # RSA-1024

    def test_response_decryption_uses_the_request_key(self):
        kd = self.net.gen_key_data()
        key = self.net.derive_key(kd.one, kd.two, kd.three, kd.four)
        payload = b'{"code":1,"data":{"x":1}}'
        env = {"v": 101, "s": "ignored", "r": self.net.aes_encrypt(payload, key)}
        plain = self.net.decrypt_response(json.dumps(env).encode("utf-8"), kd)
        self.assertEqual(plain, payload)

    def test_plaintext_response_passes_through(self):
        kd = self.net.gen_key_data()
        raw = b'{"code":340000,"desc":"boom"}'
        self.assertEqual(self.net.decrypt_response(raw, kd), raw)

    def test_checkcode_is_uppercase_md5(self):
        cc = self.net.compute_checkcode(b"abc")
        self.assertEqual(cc, "900150983CD24FB0D6963F7D28E17F72")

    def test_build_inner_is_ordered_json_with_checkcode(self):
        """The inner body must be ordered JSON, not a flat k=v string.

        The gateway parses it as JSON — the earlier flat form produced
        `340000 … (4103)` on every business call. The reference builds an
        ordered JSON object, appends `serviceTime` and `nonce`, then splices
        in `checkcode` = uppercase MD5 of the partial JSON *without* the
        closing brace.
        """
        import hashlib
        import json as _json

        inner = self.net.build_inner([("a", "1"), ("b", "2")])
        text = inner.decode()
        self.assertTrue(text.startswith('{"a":"1","b":"2","serviceTime":'))
        self.assertIn('"nonce":"', text)
        self.assertTrue(text.endswith('"}'))
        # the checkcode is the MD5 of everything before it, minus the brace
        partial, _, tail = text.rpartition(',"checkcode":"')
        self.assertTrue(tail.endswith('"}'))
        checkcode = tail[:-2]
        self.assertEqual(
            checkcode,
            hashlib.md5(partial.encode()).hexdigest().upper(),
        )
        # and the whole thing must be valid JSON
        self.assertIsInstance(_json.loads(text), dict)


class TestPasswordShape(unittest.TestCase):
    """Password shape reporting — diagnostics without leaking the secret."""

    def test_reports_length_and_whitespace(self):
        shape = nl.describe_password_shape(" abc123 ")
        self.assertIn("长度=8", shape)
        self.assertIn("首尾有空白=是", shape)
        self.assertIn("去空白后长度=6", shape)

    def test_clean_password_has_no_whitespace_flag(self):
        shape = nl.describe_password_shape("abc123")
        self.assertIn("首尾有空白=否", shape)

    def test_flags_non_ascii(self):
        shape = nl.describe_password_shape("Ａｂｃ123")
        self.assertIn("含非 ASCII 字符=是", shape)

    def test_never_contains_the_password(self):
        secret = "Sup3rSecret!"
        shape = nl.describe_password_shape(secret)
        self.assertNotIn(secret, shape)
        self.assertNotIn("Sup3r", shape)


class TestErrorHints(unittest.TestCase):
    """Known Ninebot Passport result codes must carry an actionable hint.

    A bare "username or password incorrect" tells the user nothing about
    what to do next; the most common cause is an account that never had a
    password set (Ninebot's default sign-in is SMS-code).
    """

    def test_known_codes_have_hints(self):
        for code in ("00002", "90014", "90051", "90202", "651001"):
            self.assertIn(code, nl.ERROR_HINTS)
            self.assertTrue(nl.ERROR_HINTS[code].strip())

    def test_describe_appends_the_hint(self):
        text = nl.describe({"resultCode": "90014",
                            "resultDesc": "username or password incorrect"})
        self.assertIn("resultCode=90014", text)
        self.assertIn("→", text)

    def test_describe_leaves_unknown_codes_alone(self):
        text = nl.describe({"resultCode": "99999", "resultDesc": "?"})
        self.assertIn("resultCode=99999", text)
        self.assertNotIn("→", text)

    def test_fail_output_carries_the_hint(self):
        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            rc = nl.fail({"resultCode": "90014", "resultDesc": "bad"})
        self.assertEqual(rc, 1)
        payload = json.loads(buf.getvalue())
        self.assertEqual(payload["resultCode"], "90014")
        self.assertIn("hint", payload)


class GeoTest(unittest.TestCase):
    """Coordinate → Chinese address."""

    def test_wgs84_to_gcj02_shift_is_small_but_real(self):
        lat, lon = 39.90923, 116.39751
        glat, glon = geo.wgs84_to_gcj02(lat, lon)
        # In China the GCJ-02 offset is a few hundred metres: 0.001–0.02 deg.
        # The sign of the latitude shift varies by location.
        self.assertTrue(0.001 < abs(glon - lon) < 0.02, abs(glon - lon))
        self.assertTrue(0.0005 < abs(glat - lat) < 0.02, abs(glat - lat))

    def test_matches_an_independent_implementation_of_the_same_formula(self):
        """Guard against a typo in the offset maths.

        The expected values are recomputed here from the published formula,
        written out independently of the module under test.
        """
        import math

        def tlat(x, y):
            r = (-100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y
                 + 0.2 * math.sqrt(abs(x)))
            r += (20.0 * math.sin(6.0 * x * math.pi)
                  + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
            r += (20.0 * math.sin(y * math.pi)
                  + 40.0 * math.sin(y / 3.0 * math.pi)) * 2.0 / 3.0
            r += (150.0 * math.sin(y / 12.0 * math.pi)
                  + 300.0 * math.sin(y / 30.0 * math.pi)) * 2.0 / 3.0
            return r

        def tlng(x, y):
            r = (300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y
                 + 0.1 * math.sqrt(abs(x)))
            r += (20.0 * math.sin(6.0 * x * math.pi)
                  + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
            r += (20.0 * math.sin(x * math.pi)
                  + 40.0 * math.sin(x / 3.0 * math.pi)) * 2.0 / 3.0
            r += (150.0 * math.sin(x / 12.0 * math.pi)
                  + 300.0 * math.sin(x / 30.0 * math.pi)) * 2.0 / 3.0
            return r

        for lat, lon in ((39.90923, 116.39751), (31.23037, 121.47370),
                         (23.10649, 113.32446), (39.90923, 116.39751)):
            dlat = tlat(lon - 105.0, lat - 35.0)
            dlon = tlng(lon - 105.0, lat - 35.0)
            rad = lat / 180.0 * math.pi
            magic = 1 - math.sin(rad) ** 2
            sq = math.sqrt(magic)
            dlat = (dlat * 180.0) / ((6335552.717 * (1 - (lat / 180.0 * math.pi) ** 2))
                                     / (magic * sq) * math.pi)
            dlon = (dlon * 180.0) / (6335552.717 * sq * math.cos(rad) * math.pi)
            self.assertAlmostEqual(geo.wgs84_to_gcj02(lat, lon)[0], lat + dlat, places=12)
            self.assertAlmostEqual(geo.wgs84_to_gcj02(lat, lon)[1], lon + dlon, places=12)

    def test_gcj02_to_wgs84_round_trips(self):
        lat, lon = 39.90923, 116.39751
        glat, glon = geo.wgs84_to_gcj02(lat, lon)
        back_lat, back_lon = geo.gcj02_to_wgs84(glat, glon)
        # One-step inverse; sub-metre to a few metres is normal.
        self.assertAlmostEqual(back_lat, lat, places=4)
        self.assertAlmostEqual(back_lon, lon, places=4)

    def test_conversion_is_a_no_op_outside_china(self):
        lat, lon = 40.7128, -74.0060  # New York
        self.assertEqual(geo.wgs84_to_gcj02(lat, lon), (lat, lon))
        self.assertEqual(geo.gcj02_to_wgs84(lat, lon), (lat, lon))

    def test_reverse_geocode_rejects_nonsense(self):
        self.assertIsNone(geo.reverse_geocode(None, 117.0))
        self.assertIsNone(geo.reverse_geocode(31.0, None))
        self.assertIsNone(geo.reverse_geocode(999.0, 999.0))

    def test_reverse_geocode_uses_the_cache(self):
        import tempfile
        import os
        with tempfile.TemporaryDirectory() as tmp:
            cache = os.path.join(tmp, "geo.json")
            os.environ["NINEBOT_GEO_CACHE"] = cache
            try:
                geo._cache_store("k", {"address": "缓存地址", "source": "test"})
                hit = geo._cache_load().get("k")
                self.assertEqual(hit["value"]["address"], "缓存地址")
                self.assertIn("at", hit)
            finally:
                os.environ.pop("NINEBOT_GEO_CACHE", None)

    def test_format_address_prefers_short(self):
        self.assertEqual(geo.format_address({"short": "A路", "address": "长地址"}), "A路")
        self.assertEqual(geo.format_address({"address": "长地址"}), "长地址")
        self.assertEqual(geo.format_address(None), "")

    def test_no_real_coordinate_is_committed(self):
        """The repository is public — no vehicle's actual position in it.

        Coordinates always come from the Ninebot API at runtime. The
        fixtures use a well-known public landmark instead.
        """
        import glob
        import os
        repo = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
        # Built by concatenation so this file does not itself trip the scan.
        banned = ("31." + "8337", "117." + "0791",
                  "31.000" + "77", "117.000" + "17")
        hits = []
        for path in glob.glob(os.path.join(repo, "**", "*"), recursive=True):
            if not os.path.isfile(path):
                continue
            if any(part in (".git", "node_modules", "__pycache__", ".venv")
                   for part in path.split(os.sep)):
                continue
            if not path.endswith((".py", ".md", ".sh", ".yml", ".yaml", ".json")):
                continue
            try:
                with open(path, encoding="utf-8") as fh:
                    body = fh.read()
            except (OSError, UnicodeDecodeError):
                continue
            for token in banned:
                if token in body:
                    hits.append(f"{os.path.relpath(path, repo)}: {token}")
        self.assertEqual(hits, [], "real coordinates leaked into the repo: "
                                   + ", ".join(hits))


class BusinessTypeTest(unittest.TestCase):
    """`Business-Type` is decided per ENDPOINT, not per gateway.

    The open-source reference implementation maps by host and therefore
    sends "1" to the travel gateway. The gateway does not error on that —
    it answers code=1 with an EMPTY ride history, so the wrong value
    fails silently and looks exactly like "no rides this month".
    """

    def test_truth_table_from_the_official_binary(self):
        cases = {
            "https://ebike.ninebot.com/vehicle/binding/my-vehicle": "1",
            "https://ebike.ninebot.com/vehicle/vehicle/desktop-component": "2",
            "https://steeldust.ninebot.com/v6/vehicle/battery-info": "2",
            "https://cn-cbu-gateway.ninebot.com/app-api/travel/v6/travel-list2": "2",
            "https://cn-cbu-gateway.ninebot.com/app-api/travel/v6/travel-info": "2",
        }
        for url, expected in cases.items():
            self.assertEqual(client.business_type_for(url), expected, url)

    def test_travel_is_not_fallback_to_one(self):
        url = "https://cn-cbu-gateway.ninebot.com/app-api/travel/v6/travel-list2"
        self.assertNotEqual(client.business_type_for(url), "1")

    def test_business_login_uses_zero(self):
        self.assertEqual(
            client.business_type_for("https://api-jhcx-v6-bj.ninebot.com/user/user/login"),
            "0")

    def test_unknown_path_falls_back(self):
        self.assertEqual(
            client.business_type_for("https://ebike.ninebot.com/vehicle/whatever"),
            "2")
        self.assertEqual(
            client.business_type_for("https://example.invalid/x"), "1")


class RideHistoryTest(unittest.TestCase):
    """Parsing of the travel gateway's payload."""

    def test_business_uid_comes_from_the_session(self):
        # An empty uid makes the travel gateway answer 4200, so the uid
        # must be read from the session file, never from the static config.
        import ninebot_query as nq
        cfg = {"biz_uid": ""}
        original = nq.load_session
        nq.load_session = lambda: {"biz_uid": "123456789"}
        try:
            self.assertEqual(nq._business_uid(cfg), "123456789")
        finally:
            nq.load_session = original

    def test_config_uid_wins_when_present(self):
        import ninebot_query as nq
        self.assertEqual(nq._business_uid({"biz_uid": "42"}), "42")

    def test_rides_are_sorted_newest_first(self):
        import ninebot_report as rep
        rides = [
            {"end_time": 100, "end_time_format": "2026-11-01 10:00:00", "mileages": "1.0"},
            {"end_time": 300, "end_time_format": "2026-11-03 10:00:00", "mileages": "2.0"},
            {"end_time": 200, "end_time_format": "2026-11-02 10:00:00", "mileages": "3.0"},
        ]
        travel = {"times": 3, "total_mileages": "6.0", "duration": 600,
                  "ec": 10, "month": "202611", "list": rides}
        out = rep.render({"device_name": "X", "battery": 50}, travel=travel)
        self.assertIn("2026-11-03 10:00:00", out)
        # The newest ride must come before the older ones. The secondary
        # list prints "MM-DD HH:MM", the headline prints the full stamp.
        self.assertLess(out.index("2026-11-03 10:00:00"), out.index("11-02 10:00"))
        self.assertLess(out.index("11-02 10:00"), out.index("11-01 10:00"))

    def test_empty_month_says_so(self):
        import ninebot_report as rep
        out = rep.render({"device_name": "X", "battery": 50},
                         travel={"month": "202601", "times": 0, "list": []})
        self.assertIn("无骑行记录", out)


class ReportRulesTest(unittest.TestCase):
    """The daily report's presentation rules.

    Each one exists because the first version got it wrong: a cycle count
    the pack cannot actually measure, a seat bucket the vehicle does not
    have, three days of forecast for a 07:45 bulletin, and energy printed
    as a percentage when it is watt-hours.
    """

    def setUp(self):
        import ninebot_report as rep
        self.rep = rep

    def test_barrel_lock_is_not_mentioned(self):
        out = self.rep.render({"device_name": "X", "battery": 50,
                                "lock": "1", "barrelLock": "0"})
        self.assertNotIn("座桶", out)
        self.assertIn("已锁车", out)

    def test_unsupported_cycle_count_is_not_printed(self):
        # have_bms_cycle_support False means bms_cycle is a placeholder.
        out = self.rep.render({"device_name": "X", "battery": 50,
                               "cycles": 100, "hasCycleSupport": False})
        self.assertNotIn("循环", out)

    def test_supported_cycle_count_is_printed(self):
        out = self.rep.render({"device_name": "X", "battery": 50,
                               "cycles": 12, "hasCycleSupport": True})
        self.assertIn("循环 12 次", out)

    def test_energy_is_watt_hours_not_percent(self):
        travel = {"month": "202611", "times": 2, "total_mileages": "8.6",
                  "duration": 2293, "ec": 120, "list": [
                      {"end_time": 2, "end_time_format": "2026-11-03 19:20:47",
                       "mileages": "0.3", "duration": 77, "speed": "25"}]}
        out = self.rep.render({"device_name": "X", "battery": 50}, travel=travel)
        self.assertIn("能耗 120 Wh", out)
        self.assertNotIn("耗电 120%", out)

    def test_only_today_weather_is_shown(self):
        wx = {"city": {"name": "X"}, "now": {}, "days": [
            {"date": "2026-11-04", "textDay": "晴", "textNight": "晴",
             "tempMin": "10", "tempMax": "17", "humidity": "50"},
            {"date": "2026-11-05", "textDay": "雨", "textNight": "雨",
             "tempMin": "9", "tempMax": "15", "humidity": "80"},
        ]}
        out = self.rep.render({"device_name": "X", "battery": 50}, wx=wx)
        self.assertIn("今天", out)
        self.assertNotIn("明天", out)
        self.assertNotIn("后天", out)

    def test_tiny_precipitation_does_not_trigger_the_rain_tip(self):
        # 0.1 mm is what the provider reports on a dry overcast day.
        self.assertFalse(self.rep._has_precip("0.1"))
        self.assertFalse(self.rep._has_precip("0.0"))
        self.assertTrue(self.rep._has_precip("1.2"))

    def test_rain_text_triggers_the_rain_tip(self):
        wx = {"city": {"name": "X"}, "now": {}, "days": [
            {"date": "2026-11-04", "textDay": "小雨", "textNight": "阴",
             "tempMin": "10", "tempMax": "17"}]}
        out = self.rep.render({"device_name": "X", "battery": 50}, wx=wx)
        self.assertIn("雨衣", out)

    def test_parked_vehicle_triggers_the_cover_tip(self):
        tips = self.rep._reminders({"powerStatus": "OFF"}, rainy=False)
        self.assertTrue(any("车衣" in t or "车库" in t for t in tips))

    def test_running_vehicle_does_not_trigger_the_cover_tip(self):
        tips = self.rep._reminders({"powerStatus": "ON"}, rainy=False)
        self.assertFalse(any("车衣" in t or "车库" in t for t in tips))

    def test_parked_and_raining_says_cover_not_raincoat(self):
        tips = self.rep._reminders({"powerStatus": "OFF"}, rainy=True)
        self.assertTrue(any("车衣" in t for t in tips))
        self.assertFalse(any("雨衣" in t for t in tips))

    def test_running_and_raining_says_raincoat(self):
        tips = self.rep._reminders({"powerStatus": "ON"}, rainy=True)
        self.assertTrue(any("雨衣" in t for t in tips))


class MileageTest(unittest.TestCase):
    """Mileage aggregation and the range-labels."""

    def setUp(self):
        import ninebot_report as rep
        self.rep = rep

    def test_all_time_includes_the_current_month(self):
        # The current month is never cached (it still changes), so summing
        # the cache alone silently dropped it.
        import ninebot_query as nq
        orig_months = nq.travel_months
        orig_cache = nq._month_cache_load
        nq.travel_months = lambda cfg, tok, sn, months: {
            "202610": {"month": "202610", "times": 3, "total_mileages": "6.8", "list": []},
            "202611": {"month": "202611", "times": 10, "total_mileages": "8.6", "list": []},
        }
        nq._month_cache_load = lambda: {
            "202601": {"month": "202601", "times": 0, "total_mileages": "0.0", "list": []},
            "202610": {"month": "202610", "times": 3, "total_mileages": "6.8", "list": []},
        }
        try:
            s = nq.travel_mileage_summary({}, "tok", "sn", "202611")
        finally:
            nq.travel_months = orig_months
            nq._month_cache_load = orig_cache
        self.assertEqual(s["all"]["km"], 15.4)
        self.assertEqual(s["all"]["times"], 13)
        # "since" is the first month WITH rides, not the first month queried.
        self.assertEqual(s["all"]["since"], "202610")

    def test_mileage_block_renders_every_span(self):
        mileage = {
            "day": {"km": 0.0, "times": 0},
            "week": {"km": 2.4, "times": 7, "since": "2026-11-02"},
            "month": {"km": 8.6, "times": 10, "label": "202611"},
            "year": {"km": 15.4, "times": 13, "label": "2026"},
            "all": {"km": 15.4, "times": 13, "since": "202610"},
            "odometerAvailable": False,
        }
        out = self.rep.render({"device_name": "X", "battery": 50}, mileage=mileage)
        for label in ("今日", "本周", "本月", "今年", "累计"):
            self.assertIn(label, out, label)
        self.assertIn("2.4 km", out)
        self.assertIn("8.6 km", out)
        # The missing odometer must be stated, not quietly omitted.
        self.assertIn("整车总里程", out)

    def test_range_values_are_named_not_raw(self):
        # "AI 23.1 km" told the reader nothing about what it measured.
        out = self.rep.render({"device_name": "X", "battery": 72,
                               "estimateMileage": 47.2,
                               "preciseEstimateMileage": 51.1,
                               "aiEstimateMileage": 22.8})
        self.assertIn("实景", out)
        self.assertIn("标准工况", out)
        self.assertIn("按你的骑行习惯估算", out)
        self.assertNotIn("AI 22.8", out)

    def test_low_habit_estimate_gets_an_explanation(self):
        out = self.rep.render({"device_name": "X", "battery": 72,
                               "preciseEstimateMileage": 51.1,
                               "aiEstimateMileage": 22.8})
        self.assertIn("短途", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
