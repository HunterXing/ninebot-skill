#!/usr/bin/env python3
"""Render the README sample board with fabricated data.

The real board carries the owner's address, coordinates and daily ride
records. None of that may reach a public repository, so the README
picture is rendered from a hand-written fixture: placeholder place
names, invented numbers, no serial number, invented ride dates.

Two things leak unless they are handled deliberately:

* the seven-day histogram reads the local ride-record cache when the
  snapshot has none, so the cache path is pointed at nothing before
  rendering — otherwise the chart shows the real rides;
* the board's timestamp is derived from the snapshot clock, so a fixed
  date would print "307 days ago". The sample uses the current time.

The official model picture is reused from the local cache: it is a public
product photo from the Ninebot CDN, and its signed URL rotates, so the
URL never reaches the repository.

Usage:  python3 scripts/make_sample_board.py [output.png]
"""
import datetime
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import ninebot_card_image as board  # noqa: E402

# Place names are deliberately vague ("某小区"); every number here is
# invented and corresponds to no real ride or reading.
#
# The ride records are anchored to today, because the histogram walks the
# last seven calendar days and matches records by date.
_TODAY = datetime.date.today()


def _fake_rides() -> list:
    """Invented rides for the last seven days, newest last."""
    plan = [(6, 0.0), (5, 4.8), (4, 0.0), (3, 7.2), (2, 1.4), (1, 3.1), (0, 0.0)]
    rides = []
    for back, km in plan:
        if km <= 0:
            continue
        day = _TODAY - datetime.timedelta(days=back)
        rides.append({
            "end_time_format": f"{day.isoformat()} 19:24:00",
            "mileages": km,
            "duration": 900,
        })
    return rides


def _sample() -> dict:
    now = time.time()
    stamp = datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S+08:00")
    days = []
    for back in range(4):
        day = _TODAY - datetime.timedelta(days=back)
        text = ["多云", "晴", "阴", "小雨"][back]
        days.append({
            "date": day.isoformat(),
            "textDay": text,
            "textNight": "阴",
            "tempMin": ["12", "13", "10", "9"][back],
            "tempMax": ["21", "23", "19", "17"][back],
            "weekday": "周" + "一二三四五六日"[day.weekday()],
        })
    return {
        "fetched_at": stamp,
        "epoch": int(now),
        "device": {
            "name": "九号电自Fz1 60",
            "sn": "",
            "imgUrl": "",          # filled in below, kept out of the repo
            "color": "温暖金",
        },
        "status": {
            "battery": "78",
            "status": 1,
            "location": "",
            "locationDetail": "",
            "locationSource": "",
            "lat": "0.0000",
            "lon": "0.0000",
            "lock": 1,
            "estimateMileage": 48.6,
            "chargingState": "未充电",
            "powerStatus": "ON",
            "voltage": "52.8",
            "temperature": "18",
            "health": 100,
            "remainChargeTime": "",
            "smartServiceExpired": 0,
            "ble_name": "",
            "device_name": "",
            "aiEstimateMileage": 22.8,
            "preciseEstimateMileage": 44.1,
        },
        "battery": {
            "battery": 78,
            "voltage": "52.8",
            "temperature": "18",
            "health": 100,
            "chargingPower": 0,
            "remainChargeTime": "",
            "batteryType": "锂离子",
            "packCount": "1",
            "isLithium": True,
            "hasCycleSupport": False,
            "cycles": None,
            "estimateMileage": None,
        },
        "geo": {
            "address": "某省某市某区",
            "short": "某小区",
            "source": "amap",
            "adcode": "000000",
            "province": "某省",
            "city": "某市",
            "district": "某区",
            "township": "某街道",
            "street": "某路",
            "houseNumber": "",
            "poi": "某小区",
            "poiDistance": 96,
            "formattedAddress": "某省某市某区某路某小区",
            "cached": False,
            "coords": {"lat": 0.0, "lon": 0.0, "system": "gcj02"},
        },
        "weather": {
            "city": {"id": "000000", "name": "某市"},
            "now": {
                "text": "多云", "temp": "18", "humidity": "62",
                "windDir": "东南风", "windScale": "2级",
                "reportTime": stamp,
            },
            "days": days,
        },
        "travel": {
            "month": _TODAY.strftime("%Y%m"), "times": 9,
            "total_mileages": "16.5", "duration": 1420, "ec": 21,
            "first_time": int(now) - 86400 * 30,
            "list": _fake_rides(), "detail": [],
        },
        "mileage": {
            "day": {"km": 0.0, "times": 0},
            "week": {"km": 16.5, "times": 4},
            "month": {"km": 16.5, "times": 4},
            "year": {"km": 16.5, "times": 4},
            "all": {"km": 128.6, "times": 37},
            "odometerAvailable": False,
        },
    }


# A visible marker: the picture travels to strangers, and invented
# numbers must not be mistakable for someone's real ride records.
_DEMO_CSS = (
    ".demo-flag{display:inline-block;margin-left:14px;vertical-align:3px;"
    "font-size:15px;font-weight:700;color:#ffd79a;"
    "background:rgba(255,196,107,.12);border:1px solid rgba(255,196,107,.34);"
    "border-radius:999px;padding:3px 12px;letter-spacing:.5px}"
)


def _mark_as_sample(page: str) -> str:
    marker = '<span class="demo-flag">示例数据</span>'
    page = page.replace('<div class="title">', '<div class="title">', 1)
    # Insert the marker right after the closing tag of the title line.
    needle = "</div>\n  <div>"
    idx = page.find(needle)
    if idx == -1:
        needle = "</div>"
        idx = page.find(needle)
    if idx != -1:
        page = page[:idx] + marker + page[idx:]
    return page.replace("</style>", _DEMO_CSS + "</style>", 1)


def main() -> int:
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        ROOT, "docs", "images", "board-sample.png")
    os.makedirs(os.path.dirname(out), exist_ok=True)

    # Keep the real ride-record cache out of the picture: the histogram
    # falls back to it whenever the snapshot has no records.
    os.environ["NINEBOT_TRAVEL_CACHE"] = os.path.join(
        ROOT, ".nonexistent-travel-cache.json")

    sample = _sample()

    # The signed picture URL is runtime-only data. Read it from the live
    # snapshot so the local photo cache stays warm, but never write it
    # into the repository.
    url = ""
    live = os.path.expanduser(
        os.environ.get("NINEBOT_SNAPSHOT",
                       "~/.hermes/state/ninebot/snapshot.json"))
    try:
        with open(live, encoding="utf-8") as fh:
            url = (json.load(fh).get("device") or {}).get("imgUrl") or ""
    except OSError:
        pass
    sample["device"]["imgUrl"] = url

    # Resolve the official picture exactly like the live renderer does:
    # local cache first, transparent-background crop second.
    photo = board._photo_path(sample)
    if photo:
        photo = board._crop_transparent(
            photo, os.path.join(os.path.dirname(photo), "model-cropped.png"))

    page = _mark_as_sample(board.build_html(sample, photo))
    board.render_png(page, out)
    _shrink(out)
    print(out)
    return 0


def _shrink(path: str) -> None:
    """Quantise the PNG so a README picture stays a sane size.

    The board is 3200x2000; as a 24-bit PNG that is over a megabyte of
    repository weight for a dark gradient interface. 256 colours with
    Floyd-Steinberg dithering keeps the gradients clean and costs about
    60% of the size — checked visually on both the card area and the
    photographic vehicle region.
    """
    try:
        from PIL import Image
    except ImportError:
        return
    # Pillow 10 moved these constants into enums; older releases keep
    # them as module-level ints. Support both.
    method = getattr(getattr(Image, "Quantize", Image), "MEDIANCUT", 0)
    dither = getattr(getattr(Image, "Dither", Image), "FLOYDSTEINBERG",
                    getattr(Image, "FLOYDSTEINBERG", 1))
    with Image.open(path) as im:
        quantised = im.convert("RGB").quantize(
            colors=256, method=method, dither=dither)
        quantised.save(path, optimize=True)


if __name__ == "__main__":
    raise SystemExit(main())
