#!/usr/bin/env python3
"""Tests for the status-board renderer.

The board is a picture, so the things worth testing are the ones a
screenshot cannot catch: a missing field must not print a wrong number,
the grid must fill every cell, and the picture must come from the
snapshot rather than a second network call.
"""
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))

import ninebot_card_image as board  # noqa: E402


def snapshot(**overrides):
    snap = {
        "fetched_at": "2026-11-04T15:38:00+08:00",
        "epoch": 1793776680,
        "device": {"name": "九号电自Fz1 60", "sn": "TEST0001",
                   "imgUrl": "https://example.invalid/model.png",
                   "color": "温暖金"},
        "status": {"battery": "72", "powerStatus": "ON",
                   "chargingState": "not_charge", "lock": 1,
                   "preciseEstimateMileage": 51.1, "estimateMileage": 47.2,
                   "aiEstimateMileage": 21.6, "voltage": "50.5",
                   "temperature": "16", "smartServiceExpired": 0},
        "battery": {},
        "geo": {"poi": "某小区", "street": "某路",
                "district": "某区", "poiDistance": 104},
        "weather": {"now": {"text": "雾", "temp": "15", "humidity": "91",
                            "windDir": "西风", "windScale": "4"},
                    "days": [{"date": "2026-11-04", "textDay": "阴",
                              "tempMin": "10", "tempMax": "17"}]},
        "travel": {"month": "202611", "times": 10, "total_mileages": "8.6",
                   "list": [{"end_time_format": "2026-11-03 19:20:47",
                             "mileages": "0.30"}]},
        "mileage": {"day": {"km": 0, "times": 0},
                    "week": {"km": 2.4, "times": 7},
                    "month": {"km": 8.6, "times": 10},
                    "all": {"km": 15.4, "times": 13},
                    "odometerAvailable": False},
    }
    snap.update(overrides)
    return snap


def body_of(snap, photo=None):
    """The rendered markup without the stylesheet.

    Assertions have to look at the body: the CSS itself contains strings
    like "0%" and ".bike-missing", which would match by accident.
    """
    html = board.build_html(snap, photo)
    return html.split("</style>")[1]


class TestBoard(unittest.TestCase):

    def test_every_figure_comes_from_the_snapshot(self):
        body = body_of(snapshot())
        for expected in ("72%", "51.1 km", "47.2 km", "21.6 km",
                         "15.4 km", "2.4 km", "8.6 km", "某小区",
                         "某路", "某区", "104 m", "雾", "15 °C",
                         "50.5 V", "温暖金"):
            self.assertIn(expected, body, f"missing {expected}")

    def test_the_serial_number_is_not_on_the_board(self):
        """The device serial identifies the vehicle, not its state — the
        owner asked for it to be left off."""
        body = body_of(snapshot())
        self.assertNotIn("TEST0001", body)
        self.assertNotIn("SN ", body)

    def test_missing_figures_print_a_dash_not_zero(self):
        snap = snapshot()
        snap["status"]["preciseEstimateMileage"] = None
        snap["status"]["battery"] = None
        snap["mileage"]["all"]["km"] = None
        body = body_of(snap)
        self.assertIn("—", body)
        self.assertNotIn("0.0 km", body)
        self.assertNotIn(">0%<", body)

    def test_grid_places_every_card_explicitly(self):
        """Auto-flow leaves the bottom-right blank; every cell is placed."""
        html = board.build_html(snapshot(), None)
        for cls in ("c-battery", "c-mileage", "c-weather",
                    "c-range", "c-power", "c-location"):
            self.assertIn(f'class="{cls}"', html)
        self.assertIn("grid-column:1;grid-row:3", html)
        self.assertIn("grid-column:3;grid-row:3", html)
        self.assertIn("grid-column:2;grid-row:1 / span 3", html)

    def test_the_vehicle_owns_the_centre_column(self):
        body = body_of(snapshot())
        bike = body.split('class="bike-cell"')[1].split('class="c-range"')[0]
        self.assertIn("bike-wrap", bike)
        self.assertIn("week", bike)

    def test_picture_comes_from_the_snapshot_url(self):
        body = body_of(snapshot(), "/tmp/model.png")
        self.assertIn("file:///tmp/model.png", body)
        self.assertNotIn("bike-missing", body)

    def test_missing_picture_still_renders_the_figures(self):
        body = body_of(snapshot(), None)
        self.assertIn("bike-missing", body)
        self.assertIn("72%", body)

    def test_picture_url_is_never_hardcoded(self):
        """The signed URL changes; only the snapshot may carry it."""
        html = board.build_html(snapshot(), None)
        self.assertNotIn("oms-oss-public.ninebot.com", html)
        self.assertNotIn("nbchecksignv1", html)

    def test_stale_snapshot_is_labelled(self):
        snap = snapshot(stale=True, error="boom")
        body = body_of(snap)
        self.assertIn("stale", body)
        self.assertIn("刷新失败", body)

    def test_three_range_figures_each_carry_their_caliber(self):
        body = body_of(snapshot())
        self.assertIn("续航（实景）", body)
        self.assertIn("标准工况", body)
        self.assertIn("按骑行习惯", body)

    def test_battery_temperature_is_not_mistaken_for_ambient(self):
        body = body_of(snapshot())
        self.assertIn("电池", body)

    def test_odometer_label_is_honest(self):
        body = body_of(snapshot())
        self.assertIn("累计骑行", body)
        self.assertNotIn("累计（仪表盘）", body)

    def test_odometer_flag_is_read_from_the_mileage_top_level(self):
        """It lives beside "all", not inside it — a wrong nesting level
        silently always reports "no odometer"."""
        snap = snapshot()
        snap["mileage"]["odometerAvailable"] = True
        body = body_of(snap)
        self.assertIn("累计（仪表盘）", body)
        self.assertNotIn("累计骑行", body)

    def test_week_bars_summarise_the_days(self):
        snap = snapshot()
        snap["travel"]["list"] = [
            {"end_time_format": "2026-11-03 10:00:00", "mileages": "1.5"},
            {"end_time_format": "2026-11-03 18:00:00", "mileages": "0.5"},
        ]
        html = board._week_bars(snap)
        self.assertIn("近 7 日骑行", html)
        self.assertIn("共 2.0 km", html)

    def test_week_bars_are_empty_without_records(self):
        snap = snapshot(travel={"list": []})
        self.assertEqual("", board._week_bars(snap))

    def test_behaviour_tip_appears_when_the_bike_is_parked_in_rain(self):
        """The daily report's tips must survive the switch to a picture."""
        snap = snapshot()
        snap["status"]["powerStatus"] = "OFF"
        snap["weather"]["days"][0]["textDay"] = "小雨"
        body = body_of(snap)
        self.assertIn("tips", body)
        self.assertIn("车衣", body)

    def test_no_tip_strip_when_there_is_nothing_to_say(self):
        snap = snapshot()
        snap["status"]["powerStatus"] = "ON"
        snap["weather"]["days"][0]["textDay"] = "阴"
        self.assertEqual("", board._reminder_strip(snap))

    def test_rain_comes_from_the_forecast_text_not_the_rainfall_number(self):
        """A weather API reports 0.1 mm on overcast days; the text is the
        only reliable rain signal."""
        snap = snapshot()
        snap["status"]["powerStatus"] = "OFF"
        snap["weather"]["days"][0]["textDay"] = "阴"
        snap["weather"]["now"]["precip"] = "0.1"
        strip = board._reminder_strip(snap)
        self.assertNotIn("有雨", strip)
        self.assertNotIn("雨衣", strip)

    def test_html_escapes_hostile_values(self):
        snap = snapshot()
        snap["geo"]["poi"] = "<script>alert(1)</script>"
        html = board.build_html(snap, None)
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_board_size_is_landscape(self):
        """A landscape board gives the 1.5:1 picture room to breathe."""
        self.assertGreater(board.WIDTH, board.HEIGHT)


if __name__ == "__main__":
    unittest.main(verbosity=2)
