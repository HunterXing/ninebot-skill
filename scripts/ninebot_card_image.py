#!/usr/bin/env python3
"""Render the vehicle status as one picture: the bike in the middle, the
state around it.

Why a picture
-------------
Text answers a "how is the bike" question fine, but on a phone a status
board reads faster than a paragraph: one glance for the battery, one for
where the bike is parked. This renders that board.

Why the bike photo comes from the API
-------------------------------------
The account channel returns the official model picture for the exact trim
(`img_url` / `v6_dark_img_url`). Asking the API keeps two promises:

  * the picture matches the vehicle (colour and model are the ones the
    owner actually owns), and
  * there is no second fetch path — the URL travels in the snapshot, so
    rendering stays a pure, offline step.

The URL carries a signed query parameter, so it must not be hardcoded and
must not be committed. It is fetched at runtime and cached on disk.

Layout
------
Square-ish 1080x1440 board: the vehicle photo occupies the centre band,
status cards ring it (battery top-left, range top-right, power/lock
bottom-left, location bottom-right), and the weather strip closes the
bottom. Every number comes from the snapshot; nothing is recomputed here.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import subprocess
import sys
import tempfile
from typing import Any, Dict, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import ninebot_snapshot as snapstore  # noqa: E402

# Media has to land in a whitelist directory or the delivery layer drops
# it while still reporting success.
CACHE_DIR = os.path.expanduser(
    os.environ.get("NINEBOT_CARD_CACHE", "~/.hermes/cache/images"))
PHOTO_CACHE = os.path.join(
    os.path.expanduser(
        os.environ.get("NINEBOT_PHOTO_CACHE", "~/.hermes/cache/ninebot")),
    "model.png")

# The board is landscape, not square. The official model picture is a
# 1.5:1 landscape shot; inside a square board the centre column ends up
# a tall narrow strip and the bike can only fill a quarter of it (measured:
# 78% of the panel width but 25% of its height). A landscape board gives
# the hero column the width the picture actually needs.
WIDTH = 1600
HEIGHT = 1000
SCALE = 2

# Palette: dark board, warm accents. Kept here so a future redesign
# changes one place instead of hunting hex codes inside the template.
BG = "#0f141c"
CARD = "#1b2431"
CARD_EDGE = "#2a3646"
TEXT = "#e8eef6"
TEXT_DIM = "#9fb0c6"
ACCENT = "#ffc46b"      # the bike's champagne-gold frame
ACCENT_2 = "#6fd0f5"    # cool counterpoint for data
OK = "#6fdda8"
WARN = "#ffb066"


def _esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""))


def _num(value: Any) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _fmt(value: Any, unit: str = "", digits: int = 1) -> str:
    n = _num(value)
    if n is None:
        return "—"
    return f"{n:.{digits}f}{unit}"


def _battery_pct(snap: Dict[str, Any]) -> Optional[float]:
    status = snap.get("status") or {}
    return _num(status.get("battery"))


def _power_label(snap: Dict[str, Any]) -> str:
    status = snap.get("status") or {}
    value = str(status.get("powerStatus") or "").upper()
    if value == "ON":
        return "已开机"
    if value == "OFF":
        return "已关机"
    return "未知"


def _charging_label(snap: Dict[str, Any]) -> str:
    status = snap.get("status") or {}
    value = str(status.get("chargingState") or "")
    return {
        "charging": "充电中",
        "charge": "充电中",
        "full": "已充满",
        "not_charge": "未充电",
    }.get(value, "未知")


def _lock_label(snap: Dict[str, Any]) -> str:
    status = snap.get("status") or {}
    return "已锁车" if str(status.get("lock")) in ("1", "True") else "未锁车"


def _week_bars(snap: Dict[str, Any]) -> str:
    """A seven-day ride histogram for the hero panel.

    The board's centre column is wide and tall; without something in it
    the vehicle floats in an empty box. A real chart of the last seven
    days fills that space with information rather than decoration.

    Days come from the ride records in the snapshot plus the cached
    earlier months, so a day that was ridden in the previous month still
    shows up.
    """
    import datetime
    per_day: Dict[str, float] = {}

    def add(records: List[Dict[str, Any]]) -> None:
        for rec in records or []:
            stamp = str(rec.get("end_time_format") or "")[:10]
            if stamp:
                per_day[stamp] = per_day.get(stamp, 0.0) + float(
                    rec.get("mileages") or 0)

    add((snap.get("travel") or {}).get("list") or [])
    # Earlier months live in the local month cache (the collector skips
    # months it already walked past).
    cache_path = os.path.expanduser(
        os.environ.get("NINEBOT_TRAVEL_CACHE",
                       "~/.hermes/cache/ninebot_travel_months.json"))
    try:
        with open(cache_path, encoding="utf-8") as fh:
            for month in json.load(fh).values():
                add(month.get("list") or [])
    except (OSError, ValueError):
        pass

    today = datetime.date.today()
    days: List[datetime.date] = []
    for back in range(6, -1, -1):
        day = today - datetime.timedelta(days=back)
        days.append(day)
    if not per_day:
        return ""
    peak = max([per_day.get(d.isoformat(), 0.0) for d in days] or [0.0]) or 1.0
    bars = ""
    for day in days:
        km = per_day.get(day.isoformat(), 0.0)
        height = int(round(km / peak * 100)) if km else 0
        klass = "bar-fill" if km else "bar-empty"
        style = f"height:{max(height, 4)}%" if km else "height:4%"
        # The day number, not the weekday initial: a week has two of each
        # initial, so "T" alone cannot be read.
        bars += (f'<div class="bar-col"><div class="bar-track">'
                 f'<div class="{klass}" style="{style}"></div></div>'
                 f'<div class="bar-day">{day.day}</div>'
                 f'<div class="bar-km">{km:.1f}</div></div>')
    total = sum(per_day.get(d.isoformat(), 0.0) for d in days)
    span = f"{days[0].month}/{days[0].day}–{days[-1].month}/{days[-1].day}"
    return (f'<div class="week"><div class="week-head">'
            f'<span>近 7 日骑行 <span class="week-span">{span}</span></span>'
            f'<span class="week-total">共 {total:.1f} km</span></div>'
            f'<div class="bars">{bars}</div></div>')


def _reminder_strip(snap: Dict[str, Any]) -> str:
    """The behaviour tips, if any.

    Wording and rain rule come from the daily report module — one place
    decides what "parked in the rain" means. The board does not invent
    its own phrasing, otherwise the two outputs drift apart.
    """
    import ninebot_report as report
    status = snap.get("status") or {}
    weather = snap.get("weather") or {}
    days = weather.get("days") or []
    today = days[0] if days else {}
    text_today = str(today.get("textDay") or "")
    rainy = ("雨" in text_today) or ("雪" in text_today)
    tips = report.reminders(status, rainy)
    if not tips:
        return ""
    items = "".join(f'<span class="tip">{_esc(t)}</span>' for t in tips)
    return f'<div class="tips">{items}</div>'


def _photo_path(snap: Dict[str, Any]) -> Optional[str]:
    """Fetch and cache the official model picture.

    Returns a local path, or None when the picture cannot be obtained —
    the board still renders without it, because a missing photo must not
    cost the user the status numbers.
    """
    device = snap.get("device") or {}
    url = device.get("imgUrl") or device.get("img_url") or ""
    if not url:
        return None
    if os.path.exists(PHOTO_CACHE) and os.path.getsize(PHOTO_CACHE) > 1024:
        # Cache hit — but only trust it when the URL still matches, so a
        # re-signed URL refreshes the picture.
        stamp = os.path.join(os.path.dirname(PHOTO_CACHE), "model.url")
        try:
            with open(stamp, encoding="utf-8") as fh:
                if fh.read().strip() == url:
                    return PHOTO_CACHE
        except OSError:
            pass
    try:
        import urllib.request
        # The Ninebot CDN is domestic; the local proxy must be bypassed
        # for it or the connection gets reset.
        for var in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
                    "all_proxy", "ALL_PROXY"):
            os.environ.pop(var, None)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url, timeout=20) as resp:
            data = resp.read()
        if len(data) < 1024:
            return None
        os.makedirs(os.path.dirname(PHOTO_CACHE), exist_ok=True)
        tmp = PHOTO_CACHE + ".tmp"
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, PHOTO_CACHE)
        with open(os.path.join(os.path.dirname(PHOTO_CACHE), "model.url"),
                  "w", encoding="utf-8") as fh:
            fh.write(url)
        return PHOTO_CACHE
    except Exception:  # noqa: BLE001 - a photo is a bonus, never a blocker
        if os.path.exists(PHOTO_CACHE):
            return PHOTO_CACHE
        return None


def _crop_transparent(src: str, dest: str) -> str:
    """Trim the transparent margin around the model picture."""
    try:
        from PIL import Image
        im = Image.open(src).convert("RGBA")
        bbox = im.getchannel("A").getbbox()
        if bbox:
            im = im.crop(bbox)
        im.save(dest)
        return dest
    except Exception:  # noqa: BLE001
        return src


def _card(title: str, value: str, rows: Optional[List[str]] = None,
          accent: str = ACCENT) -> str:
    """One status card: a label, a big value, then optional detail rows."""
    body = ""
    for row in rows or []:
        body += f'<div class="row">{_esc(row)}</div>'
    return (
        f'<div class="card">'
        f'<div class="label">{_esc(title)}</div>'
        f'<div class="value" style="color:{accent}">{_esc(value)}</div>'
        f'{body}'
        f'</div>'
    )


def _battery_card(snap: Dict[str, Any]) -> str:
    pct = _battery_pct(snap)
    status = snap.get("status") or {}
    if pct is None:
        return _card("电量", "—")
    filled = max(0, min(20, int(round(pct / 100.0 * 20))))
    bar = ('<span class="on"></span>' * filled +
           '<span class="off"></span>' * (20 - filled))
    return (f'<div class="card">'
            f'<div class="label">电量</div>'
            f'<div class="value" style="color:{ACCENT}">{pct:.0f}%</div>'
            f'<div class="bar">{bar}</div>'
            f'<div class="row">{_esc(_fmt(status.get("voltage"), " V", 1))}'
            f'　电池 {_esc(_fmt(status.get("temperature"), " °C", 0))}</div>'
            f'</div>')


def _range_card(snap: Dict[str, Any]) -> str:
    status = snap.get("status") or {}
    precise = _num(status.get("preciseEstimateMileage"))
    if precise is None:
        return _card("续航", "—")
    # Three numbers on one card must each carry its caliber, or the
    # headline reads as if it were the sum of the other two.
    rows = [f"标准工况 {_fmt(status.get('estimateMileage'), ' km', 1)}",
            f"按骑行习惯 {_fmt(status.get('aiEstimateMileage'), ' km', 1)}"]
    return _card("续航（实景）", f"{precise:.1f} km", rows, ACCENT_2)


def _power_card(snap: Dict[str, Any]) -> str:
    power = _power_label(snap)
    charging = _charging_label(snap)
    lock = _lock_label(snap)
    smart = (snap.get("status") or {}).get("smartServiceExpired")
    chip_color = OK if power == "已开机" else TEXT_DIM
    smart_html = ('<span class="chip" style="color:%s">智能服务在保</span>'
                  % OK if not smart else
                  '<span class="chip" style="color:%s">智能服务到期</span>' % WARN)
    return (f'<div class="card">'
            f'<div class="label">状态</div>'
            f'<div class="chips">'
            f'<span class="chip" style="color:{chip_color}">● {power}</span>'
            f'<span class="chip" style="color:{TEXT_DIM}">○ {charging}</span>'
            f'<span class="chip" style="color:{TEXT_DIM}">🔒 {lock}</span>'
            f'{smart_html}'
            f'</div></div>')


def _mileage_card(snap: Dict[str, Any]) -> str:
    mileage = snap.get("mileage") or {}
    week = mileage.get("week") or {}
    month = mileage.get("month") or {}
    allm = mileage.get("all") or {}
    rows = [f"本周 {_fmt(week.get('km'), ' km', 1)}",
            f"本月 {_fmt(month.get('km'), ' km', 1)}"]
    # The flag sits at the top of the mileage block, not inside "all".
    if mileage.get("odometerAvailable"):
        rows.insert(0, "累计（仪表盘）")
    else:
        # The odometer field is null on a shared account. The cumulative
        # ride log is still the life-to-date figure for a car whose first
        # record is not older than its activation date — but that is a
        # per-vehicle judgement, so the board labels it honestly.
        rows.insert(0, "累计骑行")
    return _card("里程", f"{_fmt(allm.get('km'), ' km', 1)}", rows, ACCENT_2)


def _location_card(snap: Dict[str, Any]) -> str:
    geo = snap.get("geo") or {}
    status = snap.get("status") or {}
    poi = str(geo.get("poi") or "").strip()
    if not poi:
        # No POI resolved: fall back to the short address, cut to the
        # first two segments so it stays on one or two lines.
        parts = [p.strip() for p in
                 str(status.get("location") or "").split("·") if p.strip()]
        poi = " · ".join(parts[:2]) or "位置未知"
    rows = []
    bits = [str(geo.get(k) or "").strip() for k in ("street", "district")]
    bits = [b for b in bits if b]
    if bits:
        rows.append(" · ".join(bits))
    if geo.get("poiDistance"):
        rows.append(f"距路侧 {int(geo['poiDistance'])} m")
    return _card("位置", poi, rows)


def _weather_card(snap: Dict[str, Any]) -> str:
    weather = snap.get("weather") or {}
    now = weather.get("now") or {}
    days = weather.get("days") or []
    today = days[0] if days else {}
    if not now:
        return _card("天气", "—")
    rows = []
    if today:
        rows.append(f"今日 {_esc(today.get('textDay') or '')} "
                    f"{_esc(today.get('tempMin') or '')}~"
                    f"{_esc(today.get('tempMax') or '')} °C")
    rows.append(f"湿度 {_esc(now.get('humidity') or '—')}% · "
                f"{_esc(now.get('windDir') or '')}{_esc(now.get('windScale') or '')}级")
    value = f"{_esc(now.get('text') or '')} {_esc(now.get('temp') or '')} °C"
    return _card("天气", value, rows, ACCENT_2)


def _stamp_text(snap: Dict[str, Any]) -> str:
    fetched = str(snap.get("fetched_at") or "")
    clock = fetched[11:16] if len(fetched) >= 16 else "—"
    age = snapstore.age_text(snapstore.age_seconds(snap))
    return f"{clock} · {age}" if age != "刚刚" else clock


def build_html(snap: Dict[str, Any], photo: Optional[str]) -> str:
    device = snap.get("device") or {}
    name = device.get("name") or "九号电动车"
    color = device.get("color") or ""
    stale = ""
    if snap.get("stale"):
        stale = (f'<div class="stale">⚠ 数据来自 '
                 f'{_esc(snapstore.age_text(snapstore.age_seconds(snap)))}，'
                 f'刷新失败</div>')

    if photo:
        photo_html = (f'<img class="bike" src="file://{_esc(photo)}" '
                      f'alt="{_esc(name)}">')
    else:
        photo_html = '<div class="bike-missing">车型图不可用</div>'

    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><style>
*{{margin:0;padding:0;box-sizing:border-box}}
html,body{{width:{WIDTH}px;height:{HEIGHT}px;overflow:hidden}}
body{{
  background:
    radial-gradient(1200px 700px at 50% 42%, #1e2a38 0%, {BG} 68%),
    {BG};
  color:{TEXT};
  font-family:"Noto Sans CJK SC","Noto Sans SC",sans-serif;
  padding:36px 40px 34px;
  display:flex;flex-direction:column;
}}
.head{{display:flex;align-items:baseline;justify-content:space-between;
  flex:0 0 auto}}
.title{{font-size:34px;font-weight:700;letter-spacing:.5px}}
.title .tint{{color:{ACCENT}}}
.subtitle{{font-size:17px;color:{TEXT_DIM};margin-top:6px}}
.stamp{{font-size:17px;color:{TEXT_DIM}}}
.stale{{margin-top:10px;font-size:16px;color:{WARN}}}

/* The board is a 3x3 grid, every cell filled: the left column carries
   电量/里程/天气, the right column 续航/状态/位置, and the vehicle spans
   the middle column across all three rows so it is the real centre.
   Placement is explicit — auto-flow around the spanning cell leaves
   empty corners (measured: the vehicle drifted off-centre and the
   bottom-right quadrant went blank). */
.stage{{flex:1 1 auto;display:grid;grid-template-columns:1fr 2.15fr 1fr;
  grid-template-rows:1fr 1fr 1fr;gap:15px;margin:15px 0 13px;min-height:0}}
/* Each status card sits in a wrapper so it can be placed explicitly;
   the wrapper stretches and the card fills it. Without the stretch the
   cards keep their content height and all the slack piles up under
   them, which reads as a half-empty board. */
.c-battery,.c-mileage,.c-weather,.c-range,.c-power,.c-location{{
  display:flex;min-height:0}}
.c-battery>.card,.c-mileage>.card,.c-weather>.card,
.c-range>.card,.c-power>.card,.c-location>.card{{flex:1 1 auto}}
.c-battery{{grid-column:1;grid-row:1}}
.c-mileage{{grid-column:1;grid-row:2}}
.c-weather{{grid-column:1;grid-row:3}}
.c-range{{grid-column:3;grid-row:1}}
.c-power{{grid-column:3;grid-row:2}}
.c-location{{grid-column:3;grid-row:3}}
.bike-cell{{grid-column:2;grid-row:1 / span 3;display:flex;flex-direction:column;
  align-items:center;justify-content:center;gap:14px;
  background:linear-gradient(180deg, #222f40 0%, #19222e 100%);
  border:1px solid {CARD_EDGE};border-radius:26px;padding:24px 28px;
  min-height:0}}
/* The picture needs its own box: as a bare flex item an <img> cannot
   resolve max-height against the column, so it stays small and the hero
   panel reads as mostly empty. */
.bike-wrap{{flex:1 1 auto;min-height:0;width:100%;display:flex;
  align-items:center;justify-content:center}}
.bike{{max-width:100%;max-height:100%;object-fit:contain;
  filter:drop-shadow(0 12px 26px rgba(0,0,0,.6))}}
.bike-missing{{font-size:20px;color:{TEXT_DIM};padding:90px 0}}
/* Trim badge above the picture. The model name lives in the board
   header, so repeating it here would be noise. */
.bike-badge{{flex:0 0 auto;font-size:16px;font-weight:600;color:{ACCENT};
  background:rgba(255,196,107,.12);border:1px solid rgba(255,196,107,.32);
  border-radius:999px;padding:5px 16px}}

/* Seven-day ride histogram — real data, filling the hero column. */
.week{{flex:0 0 auto;width:100%;margin-top:6px;
  border-top:1px solid {CARD_EDGE};padding-top:14px}}
.week-head{{display:flex;justify-content:space-between;align-items:baseline;
  font-size:16px;color:{TEXT_DIM};margin-bottom:10px}}
.week-total{{color:{ACCENT_2};font-weight:600}}
.bars{{display:flex;align-items:flex-end;gap:12px;height:96px}}
.bar-col{{flex:1;display:flex;flex-direction:column;align-items:center;
  height:100%;justify-content:flex-end}}
.bar-track{{flex:1;width:100%;display:flex;align-items:flex-end}}
.bar-fill{{width:100%;background:linear-gradient(180deg, {ACCENT} 0%,
  #d98f2e 100%);border-radius:6px 6px 3px 3px}}
.bar-empty{{width:100%;background:#2b3746;border-radius:6px 6px 3px 3px}}
.bar-day{{font-size:16px;color:{TEXT_DIM};margin-top:8px;font-weight:600}}
.bar-km{{font-size:15px;color:{TEXT};font-weight:600;margin-top:2px}}
.card{{background:{CARD};border:1px solid {CARD_EDGE};border-radius:20px;
  padding:16px 20px;display:flex;flex-direction:column;justify-content:center;
  min-height:0;overflow:hidden}}
.label{{font-size:15px;color:{TEXT_DIM};letter-spacing:2px;margin-bottom:6px}}
.value{{font-size:29px;font-weight:700;line-height:1.2;overflow-wrap:anywhere}}
.row{{font-size:15px;color:{TEXT_DIM};margin-top:6px;line-height:1.4}}
.bar{{display:flex;gap:3px;margin-top:9px}}
.bar span{{width:8px;height:8px;border-radius:3px;flex:0 0 auto}}
.bar .on{{background:{ACCENT}}}
.bar .off{{background:#34414f}}
.chips{{display:flex;flex-wrap:wrap;gap:6px 12px}}
.chip{{font-size:17px;font-weight:600;white-space:nowrap}}

/* Behaviour tips — only present when there is something to say.
   A full-width band, not a floating pill: the text is longer than one
   column, and a pill that overruns the column breaks the grid. */
.tips{{flex:0 0 auto;display:flex;flex-direction:column;gap:8px;
  margin-top:15px}}
.tip{{font-size:17px;color:#ffd79a;background:rgba(255,196,107,.10);
  border:1px solid rgba(255,196,107,.28);border-radius:14px;
  padding:11px 18px;font-weight:600;line-height:1.45}}
</style></head><body>
<div class="head">
  <div>
    <div class="title"><span class="tint">🛵</span> {_esc(name)}</div>
  </div>
  <div class="stamp">{_esc(_stamp_text(snap))}</div>
</div>
{stale}
<div class="stage">
  <div class="c-battery">{_battery_card(snap)}</div>
  <div class="c-mileage">{_mileage_card(snap)}</div>
  <div class="c-weather">{_weather_card(snap)}</div>
  <div class="bike-cell">
    <div class="bike-badge">{_esc(color) if color else name}</div>
    <div class="bike-wrap">{photo_html}</div>
    {_week_bars(snap)}
  </div>
  <div class="c-range">{_range_card(snap)}</div>
  <div class="c-power">{_power_card(snap)}</div>
  <div class="c-location">{_location_card(snap)}</div>
</div>
{_reminder_strip(snap)}
</body></html>
"""


def render_png(html_text: str, out_path: str) -> str:
    """Turn the HTML into a PNG with headless Chrome.

    The board has a fixed size, so the window size is known up front and
    Chrome's own --screenshot is enough — no CDP round trip needed.
    """
    chrome = os.environ.get("NINEBOT_CHROME") or shutil_which("google-chrome") \
        or shutil_which("google-chrome-stable") or shutil_which("chromium")
    if not chrome:
        raise RuntimeError("未找到 Chrome（google-chrome），无法出图")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        page = os.path.join(tmp, "board.html")
        with open(page, "w", encoding="utf-8") as fh:
            fh.write(html_text)
        cmd = [
            chrome, "--headless=new", "--no-sandbox", "--disable-gpu",
            "--hide-scrollbars", "--force-color-profile=srgb",
            "--font-render-hinting=none", "--disable-lcd-text",
            f"--force-device-scale-factor={SCALE}",
            f"--window-size={WIDTH},{HEIGHT}",
            f"--screenshot={out_path}", page,
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if not os.path.exists(out_path) or os.path.getsize(out_path) < 4096:
        raise RuntimeError(f"Chrome 截图失败: {proc.stderr.strip()[-300:]}")
    return out_path


def shutil_which(name: str) -> Optional[str]:
    for path in os.environ.get("PATH", "").split(os.pathsep):
        candidate = os.path.join(path, name)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description="九号车况状态图")
    ap.add_argument("--max-age", type=int, default=600,
                    help="可接受的最大陈旧秒数（默认 600）")
    ap.add_argument("--out", help="输出 PNG 路径")
    ap.add_argument("--html-only", action="store_true", help="只打印 HTML")
    ap.add_argument("--refresh", action="store_true", help="强制重新抓取")
    args = ap.parse_args()

    if args.refresh:
        # max_age=0 means "always try the API" — but a failing refresh
        # must not throw away the board: ensure() falls back to the last
        # snapshot and marks it stale, exactly like the text path.
        snap, _refreshed, error = snapstore.ensure(0)
        if error:
            print(f"WARN: 快照刷新失败，使用旧数据（{error}）", file=sys.stderr)
    else:
        snap, _refreshed, error = snapstore.ensure(args.max_age)
        if error:
            print(f"WARN: 快照刷新失败，使用旧数据（{error}）", file=sys.stderr)

    photo = _photo_path(snap)
    if photo:
        photo = _crop_transparent(photo, os.path.join(
            os.path.dirname(photo), "model-cropped.png"))

    page = build_html(snap, photo)
    if args.html_only:
        print(page)
        return 0

    stamp = snapstore.age_text(snapstore.age_seconds(snap)).replace(" ", "")
    # Name the file after the snapshot clock, not the age: an age in the
    # filename changes on every run and carries non-ASCII characters.
    fetched = str(snap.get("fetched_at") or "")
    token = re.sub(r"[^0-9]", "", fetched)[:12] or "now"
    out = args.out or os.path.join(
        CACHE_DIR, f"ninebot-status-{token}.png")
    render_png(page, out)
    print(f"MEDIA:{out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
