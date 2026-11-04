#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Template + snapshot-cache tests — no network, fixed fixtures.

Run:  python3 tests/test_card_templates.py

These lock in the rules that are easy to break while editing a template:
a value that the vehicle does not support must not be printed as zero,
a word must not be repeated just because two sources carry it, and the
snapshot must say how old it is whenever a refresh failed.
"""
from __future__ import annotations

import datetime
import json
import os
import sys
import tempfile
import time
import unittest
from typing import Optional
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import ninebot_card as card  # noqa: E402
import ninebot_snapshot as snapstore  # noqa: E402


def make_snapshot(epoch: Optional[int] = None, **overrides):
    now = int(time.time()) if epoch is None else epoch
    snapshot = {
        "fetched_at": datetime.datetime.fromtimestamp(now).astimezone().isoformat(timespec="seconds"),
        "epoch": now,
        "device": {"name": "测试车", "sn": "SN-TEST-1"},
        "status": {
            "battery": 72,
            "voltage": "50.3",
            "temperature": "16",
            "health": 0,
            "status": 1,
            "powerStatus": "ON",
            "chargingState": "not_charge",
            "lock": 1,
            "location": "某市某路 1 号",
            "locationDetail": "某省 某市 某区 某路 1 号",
            "locationSource": "amap",
            "preciseEstimateMileage": 51.1,
            "estimateMileage": 47.2,
            "aiEstimateMileage": 22.9,
            "lat": 31.8,
            "lon": 117.0,
            "remainChargeTime": "",
        },
        "battery": {"cycles": None, "hasCycleSupport": False, "health": 0},
        "geo": {"street": "某路", "houseNumber": "1号", "poiDistance": 102, "coord_system": "GCJ-02"},
        "weather": {
            "now": {"text": "阴", "temp": "16", "humidity": "92", "windDir": "西风", "windScale": "≤3"},
            "days": [
                {"textDay": "阴", "textNight": "多云", "tempMin": 10, "tempMax": 17},
                {"textDay": "晴", "tempMin": 11, "tempMax": 21},
            ],
            "city": {"name": "某区"},
        },
        "travel": {
            "duration": 2293,
            "ec": 120,
            "month": "2026-11",
            "list": [
                {"mileages": 0.3, "duration": 77, "speed": 25.0, "end_time": 1762000000,
                 "end_time_format": "2026-11-03 19:20:47"},
                {"mileages": 0.5, "duration": 93, "speed": 25.0, "end_time": 1761900000,
                 "end_time_format": "2026-11-03 07:36:59"},
            ],
        },
        "mileage": {
            "day": {"km": 0, "times": 0},
            "week": {"km": 2.4, "times": 7},
            "month": {"km": 8.6, "times": 10},
            "year": {"km": 15.4, "times": 13},
            "all": {"km": 15.4, "since": "202610"},
        },
    }
    snapshot.update(overrides)
    return snapshot


class TestTemplates(unittest.TestCase):
    def test_status_does_not_repeat_the_weather_word(self):
        out = card.render("status", make_snapshot())
        # "阴 16°C" already says it; a second "阴" from the daily forecast
        # is noise, not information.
        self.assertEqual(out.count("阴"), 1)

    def test_status_shows_the_core_figures(self):
        out = card.render("status", make_snapshot())
        for expected in ("72%", "51.1 km", "某市某路 1 号", "已开机", "已锁车", "2.4 km/7 次"):
            self.assertIn(expected, out)

    def test_battery_does_not_invent_zero_health(self):
        # bms_score = 0 means "not supported" on a lead-acid pack.
        out = card.render("battery", make_snapshot())
        self.assertNotIn("健康", out)
        self.assertIn("50.3 V", out)

    def test_battery_prints_health_when_supported(self):
        snap = make_snapshot()
        snap["status"]["health"] = 98
        snap["battery"]["health"] = 98
        self.assertIn("健康 98 %", card.render("battery", snap))

    def test_battery_skips_cycles_when_unsupported(self):
        out = card.render("battery", make_snapshot())
        self.assertNotIn("循环", out)

    def test_location_has_address_lock_and_coords(self):
        out = card.render("location", make_snapshot())
        for expected in ("某市某路 1 号", "门牌 某路1号", "已锁车", "31.800000, 117.000000"):
            self.assertIn(expected, out)

    def test_ride_uses_newest_ride_not_index_zero(self):
        snap = make_snapshot()
        snap["travel"]["list"].reverse()  # gateway order is not chronological
        out = card.render("ride", snap)
        newest = out.split("**最近一次**", 1)[1]
        self.assertIn("2026-11-03 19:20:47", newest)

    def test_params_keeps_fields_missing_from_status(self):
        out = card.render("params", make_snapshot())
        self.assertIn("battery.cycles", out)
        self.assertIn("battery.hasCycleSupport", out)
        self.assertIn("status.permissions", card.render(
            "params", make_snapshot(status={"permissions": {"allow_see_location": 1}})))

    def test_stale_marker_is_visible(self):
        snap = make_snapshot(epoch=int(time.time()) - 7200)
        snap["stale"] = True
        snap["error"] = "RuntimeError: boom"
        out = card.render("status", snap)
        self.assertIn("⚠️", out)
        self.assertIn("2.0 小时前", out)

    def test_stamp_shows_date_only_when_not_today(self):
        today = make_snapshot()
        self.assertNotIn(datetime.datetime.now().strftime("%m-%d"), card._stamp(today))
        yesterday = int(time.time()) - 86400
        self.assertIn(
            datetime.datetime.fromtimestamp(yesterday).strftime("%m-%d"),
            card._stamp(make_snapshot(epoch=yesterday)),
        )


class TestSnapshotStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "snapshot.json")

    def test_hot_snapshot_is_reused(self):
        snapstore.save(make_snapshot(), self.path)
        snap, refreshed, error = snapstore.ensure(600, self.path)
        self.assertFalse(refreshed)
        self.assertIsNone(error)
        self.assertEqual(snap["status"]["battery"], 72)

    def test_expired_snapshot_triggers_one_refresh(self):
        snapstore.save(make_snapshot(epoch=int(time.time()) - 3600), self.path)
        with mock.patch.object(snapstore, "refresh", return_value=make_snapshot()) as refresh:
            snap, refreshed, error = snapstore.ensure(600, self.path)
        refresh.assert_called_once()
        self.assertTrue(refreshed)
        self.assertIsNone(error)
        self.assertFalse(snap.get("stale"))

    def test_failed_refresh_falls_back_to_old_snapshot(self):
        snapstore.save(make_snapshot(epoch=int(time.time()) - 7200), self.path)
        with mock.patch.object(snapstore, "refresh", side_effect=RuntimeError("boom")):
            snap, refreshed, error = snapstore.ensure(600, self.path)
        self.assertFalse(refreshed)
        self.assertTrue(snap["stale"])
        self.assertIsNotNone(error)
        self.assertIn("boom", str(error))
        self.assertTrue(os.path.exists(self.path), "旧快照不能被失败刷新删掉")

    def test_missing_snapshot_and_failed_refresh_is_fatal(self):
        with mock.patch.object(snapstore, "refresh", side_effect=RuntimeError("boom")):
            with self.assertRaises(SystemExit) as ctx:
                snapstore.ensure(600, self.path)
        self.assertIn("快照不可用", str(ctx.exception))

    def test_snapshot_file_is_private(self):
        snapstore.save(make_snapshot(), self.path)
        mode = os.stat(self.path).st_mode & 0o777
        self.assertEqual(mode, 0o600, "快照含位置信息，必须 600")

    def test_snapshot_survives_a_torn_write(self):
        snapstore.save(make_snapshot(), self.path)
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write('{"broken": ')
        self.assertIsNone(snapstore.load(self.path), "损坏的快照应被视为无快照，而不是崩溃")


if __name__ == "__main__":
    unittest.main(verbosity=2)
