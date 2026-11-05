#!/usr/bin/env python3
"""Render a Ninebot answer from the local snapshot — no network, no LLM.

The point of this file is speed with a fixed shape. Every question about
the vehicle has an answer that is already measured in the snapshot, so
the answer should be a template fill, not a fresh investigation.

Templates:
    status    compact vehicle picture (the default for "车况")
    full      the whole daily report (same rendering as the cron job)
    battery   battery / range figures
    location  address, lock, coordinates
    ride      riding totals and recent rides
    weather   live conditions and today's forecast
    params    flat key=value dump — for "how many cycles / what voltage"
              style questions that are not a fixed template

`--json` prints the raw snapshot instead of a template, so anything not
covered here can still read the exact value without inventing one.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import ninebot_snapshot as snapstore  # noqa: E402


def _num(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fmt(value: Any, unit: str = "", digits: int = 1) -> str:
    n = _num(value)
    if n is None:
        return "—"
    if digits == 0:
        return f"{n:.0f}{unit}"
    return f"{n:.{digits}f}{unit}"


def _battery_bar(percent: Optional[float], width: int = 14) -> str:
    if percent is None:
        return ""
    filled = max(0, min(width, int(round(percent / 100.0 * width))))
    return "█" * filled + "░" * (width - filled)


def _power_text(value: Any) -> str:
    return {"ON": "🟢 已开机", "OFF": "⚫ 已关机"}.get(
        str(value or "").upper(), "⚪ " + (str(value) or "未知"))


def _charge_text(value: Any) -> str:
    return {
        "charging": "🔌 充电中",
        "charge": "🔌 充电中",
        "full": "✅ 已充满",
        "not_charge": "⚪ 未充电",
    }.get(str(value or ""), "⚪ " + (str(value) or "未知"))


def _lock_text(value: Any) -> str:
    return "🔒 已锁车" if str(value) in ("1", "True") else "🔓 未锁车"


def _stamp(snap: Dict[str, Any]) -> str:
    """`14:01（3 分钟前）` — freshness is part of the answer.

    A date only appears when the snapshot is not from today: on the same
    day the clock is enough, across days it is not.
    """
    fetched = str(snap.get("fetched_at") or "")
    clock = fetched[11:16] if len(fetched) >= 16 else "—"
    day = fetched[5:10] if len(fetched) >= 10 else ""
    import datetime
    today = datetime.datetime.now().strftime("%m-%d")
    if day and day != today:
        clock = f"{day} {clock}"
    age = snapstore.age_text(snapstore.age_seconds(snap))
    return f"{clock}（{age}）" if age != "刚刚" else clock


def _stale_line(snap: Dict[str, Any]) -> List[str]:
    if not snap.get("stale"):
        return []
    return [f"⚠️ 数据来自 {snapstore.age_text(snapstore.age_seconds(snap))}，"
            f"刷新失败：{snap.get('error') or '未知原因'}"]


def _rides(snap: Dict[str, Any]) -> List[Dict[str, Any]]:
    rides = list((snap.get("travel") or {}).get("list") or [])
    rides.sort(key=lambda r: int(r.get("end_time") or r.get("start_time") or 0),
               reverse=True)
    return rides


def _per_day(rides: List[Dict[str, Any]]) -> Dict[str, float]:
    out: Dict[str, float] = {}
    for r in rides:
        day = str(r.get("end_time_format") or "")[:10]
        if day:
            out[day] = out.get(day, 0.0) + float(r.get("mileages") or 0)
    return out


def tpl_status(snap: Dict[str, Any]) -> List[str]:
    st = snap.get("status") or {}
    battery = _num(st.get("battery"))
    mileage = snap.get("mileage") or {}
    day = (mileage.get("day") or {}).get("km")
    week = mileage.get("week") or {}
    rides = _rides(snap)
    lines: List[str] = [f"**🛵 {snap.get('device', {}).get('name') or '九号'}**　{_stamp(snap)}"]
    if battery is not None:
        lines.append(f"{_battery_bar(battery)}　**{battery:.0f}%**")
    precise = _num(st.get("preciseEstimateMileage"))
    ai = _num(st.get("aiEstimateMileage"))
    range_bits = []
    if precise is not None:
        range_bits.append(f"实景 {precise:.1f} km")
    if ai is not None:
        range_bits.append(f"习惯估算 {ai:.1f} km")
    if range_bits:
        lines.append("🛣 " + "　｜　".join(range_bits))
    lines.append(f"📍 {st.get('location') or st.get('locationDetail') or '位置未知'}")
    state_bits = [_power_text(st.get("powerStatus") or st.get("status")),
                  _charge_text(st.get("chargingState"))]
    if st.get("lock") is not None:
        state_bits.append(_lock_text(st.get("lock")))
    lines.append("⚡ " + " · ".join(state_bits))
    ride_bits = []
    if day is not None:
        ride_bits.append(f"今日 {float(day):.1f} km")
    if week:
        ride_bits.append(f"本周 {float(week.get('km') or 0):.1f} km/{week.get('times', 0)} 次")
    if rides:
        last = rides[0]
        ride_bits.append(f"最近 {str(last.get('end_time_format') or '')[5:16]} "
                         f"{float(last.get('mileages') or 0):.2f} km")
    if ride_bits:
        lines.append("🏍 " + "　｜　".join(ride_bits))
    wx = snap.get("weather") or {}
    now = wx.get("now") or {}
    days = wx.get("days") or []
    if now or days:
        bits = []
        if now:
            bits.append(f"{now.get('text', '')} {now.get('temp', '—')}°C")
        if days:
            today = days[0]
            if today.get("tempMin") and today.get("tempMax"):
                bits.append(f"{today['tempMin']}~{today['tempMax']}°C")
            # Only when it says something the live block did not — a
            # second "阴" after "阴 16°C" is noise, not information.
            if today.get("textDay") and today.get("textDay") != now.get("text"):
                bits.append(str(today["textDay"]))
        lines.append("🌡 " + "　｜　".join(bits))
    lines.extend(_stale_line(snap))
    return lines


def tpl_battery(snap: Dict[str, Any]) -> List[str]:
    st = snap.get("status") or {}
    battery = _num(st.get("battery"))
    lines = ["**🔋 电池**"]
    if battery is not None:
        lines.append(f"{_battery_bar(battery)}　**{battery:.0f}%**")
    bits = []
    for label, key, unit, digits in (
        ("电压", "voltage", " V", 1),
        ("温度", "temperature", " °C", 0),
        ("循环", "cycles", " 次", 0),
    ):
        value = st.get(key)
        if value is None and key == "cycles":
            value = (snap.get("battery") or {}).get(key)
        if value is not None:
            bits.append(f"{label} {_fmt(value, unit, digits)}")
    # A lead-acid pack reports bms_score = 0, which is "not supported",
    # not "0% healthy" — printing zero would invent a fault.
    health = _num(st.get("health"))
    if health is None:
        health = _num((snap.get("battery") or {}).get("health"))
    if health is not None and health > 0:
        bits.append(f"健康 {health:.0f} %")
    if bits:
        lines.append("　｜　".join(bits))
    precise = _num(st.get("preciseEstimateMileage"))
    est = _num(st.get("estimateMileage"))
    ai = _num(st.get("aiEstimateMileage"))
    rng = []
    if precise is not None:
        rng.append(f"实景 {precise:.1f} km")
    if est is not None:
        rng.append(f"标准 {est:.1f} km")
    if ai is not None:
        rng.append(f"习惯 {ai:.1f} km")
    if rng:
        lines.append("🛣 " + "　｜　".join(rng))
    if precise is not None and battery:
        lines.append(f"　满电理论 ≈ {precise / (battery / 100.0):.0f} km")
    remain = st.get("remainChargeTime")
    if remain:
        lines.append(f"⏳ 剩余充电 {remain} 分钟")
    lines.extend(_stale_line(snap))
    return lines


def tpl_location(snap: Dict[str, Any]) -> List[str]:
    st = snap.get("status") or {}
    geo = snap.get("geo") or {}
    lines = ["**📍 位置**", st.get("location") or st.get("locationDetail") or "位置未知"]
    bits = []
    if geo.get("street") and geo.get("houseNumber"):
        bits.append(f"门牌 {geo['street']}{geo['houseNumber']}")
    if geo.get("poiDistance") is not None:
        bits.append(f"最近 POI {geo['poiDistance']} m")
    if st.get("lock") is not None:
        bits.append(_lock_text(st.get("lock")))
    if bits:
        lines.append("　｜　".join(bits))
    lat, lon = _num(st.get("lat")), _num(st.get("lon"))
    if lat is not None and lon is not None:
        lines.append(f"坐标 {lat:.6f}, {lon:.6f}"
                     + (f"　（{geo.get('coord_system')}）" if geo.get("coord_system") else ""))
    if st.get("locationSource"):
        lines.append(f"定位来源：{st['locationSource']}")
    lines.extend(_stale_line(snap))
    return lines


def tpl_ride(snap: Dict[str, Any]) -> List[str]:
    travel = snap.get("travel") or {}
    mileage = snap.get("mileage") or {}
    rides = _rides(snap)
    lines = ["**🏍 骑行**"]
    cells = []
    for label, key in (("今日", "day"), ("本周", "week"), ("本月", "month"), ("今年", "year")):
        block = mileage.get(key) or {}
        if block:
            cells.append(f"{label} {float(block.get('km') or 0):.1f} km"
                         f"（{block.get('times', 0)} 次）")
    if cells:
        lines.append("　｜　".join(cells))
    total = mileage.get("all") or {}
    if total:
        since = str(total.get("since") or "")
        label = f"自 {since[:4]}-{since[4:6]}" if len(since) == 6 else ""
        lines.append(f"累计 {float(total.get('km') or 0):.1f} km" + (f"（{label}）" if label else ""))
    # The native odometer is a different caliber from the cumulative
    # above: the cumulative only covers the rides this account can see.
    odo = mileage.get("odometerKm")
    if mileage.get("odometerAvailable"):
        lines.append(f"里程表 {float(odo):.1f} km（整车总里程）"
                     if odo is not None else "里程表 —（整车总里程）")
    secs = int(travel.get("duration") or 0)
    if secs:
        lines.append(f"本月骑行时长 {secs // 60} 分 {secs % 60} 秒"
                     + (f"　｜　能耗 {travel.get('ec')} Wh" if travel.get("ec") else ""))
    per_day = _per_day(rides)
    if per_day:
        ordered = sorted(per_day.items())[-7:]
        lines.append("逐日：" + "　".join(f"{d[5:]} {v:.1f}km" for d, v in ordered))
    if rides:
        lines.append("**最近一次**　" + str(rides[0].get("end_time_format") or ""))
        lines.append(f"　{float(rides[0].get('mileages') or 0):.2f} km　"
                     f"{int(rides[0].get('duration') or 0)} 秒　"
                     f"均速 {rides[0].get('speed')} km/h")
        for r in rides[1:4]:
            stamp = str(r.get("end_time_format") or "")
            lines.append(f"　{stamp[5:16]}　{r.get('mileages')} km　"
                         f"{int(r.get('duration') or 0)} 秒")
    elif travel.get("error"):
        lines.append(f"取数失败：{travel['error']}")
    else:
        lines.append("本月无骑行记录")
    lines.extend(_stale_line(snap))
    return lines


def tpl_weather(snap: Dict[str, Any]) -> List[str]:
    wx = snap.get("weather") or {}
    now = wx.get("now") or {}
    days = wx.get("days") or []
    lines = ["**🌡 天气**"]
    if now:
        bits = [f"{now.get('text', '')} {now.get('temp', '—')}°C"]
        if now.get("humidity"):
            bits.append(f"湿度 {now['humidity']}%")
        if now.get("windDir"):
            bits.append(f"{now['windDir']} {now.get('windScale', '')}级")
        lines.append("　｜　".join(bits))
    for idx, day in enumerate(days[:2]):
        label = "今天" if idx == 0 else "明天"
        bits = []
        if day.get("textDay"):
            bits.append(str(day["textDay"]))
        if day.get("tempMin") and day.get("tempMax"):
            bits.append(f"{day['tempMin']}~{day['tempMax']}°C")
        if day.get("windScaleDay"):
            bits.append(f"{day.get('windDirDay', '')} {day['windScaleDay']}级")
        lines.append(f"**{label}**　" + "　｜　".join(bits))
    city = (wx.get("city") or {}).get("name")
    if city:
        lines.append(f"天气源：高德 · {city}")
    lines.extend(_stale_line(snap))
    return lines


def flatter(snap: Dict[str, Any]) -> List[tuple]:
    """Flat (key, value) pairs — one place to grep for any single figure."""
    rows: List[tuple] = []

    def walk(prefix: str, value: Any, depth: int = 0) -> None:
        if depth > 3:
            return
        if isinstance(value, dict):
            for key, sub in value.items():
                walk(f"{prefix}.{key}" if prefix else str(key), sub, depth + 1)
        elif isinstance(value, list):
            rows.append((prefix, f"{len(value)} 项"))
        else:
            rows.append((prefix, value))

    walk("", snap)
    return rows


def tpl_params(snap: Dict[str, Any]) -> List[str]:
    # Everything is kept, including duplicates between `status` and
    # `battery`: this view exists to be grepped for a single figure, and
    # hiding a field because another section repeats it means the one
    # place a caller looks can come up empty.
    rows = flatter(snap)
    width = max((len(str(k)) for k, _ in rows), default=0)
    return [f"{str(k).ljust(width)} = {v}" for k, v in rows]


TEMPLATES = {
    "status": tpl_status,
    "battery": tpl_battery,
    "location": tpl_location,
    "ride": tpl_ride,
    "weather": tpl_weather,
    "params": tpl_params,
}


def render(template: str, snap: Dict[str, Any]) -> str:
    if template == "full":
        import ninebot_report as report
        return report.render(snap.get("status") or {},
                             geo=snap.get("geo"),
                             wx=snap.get("weather"),
                             travel=snap.get("travel"),
                             mileage=snap.get("mileage"))
    fn = TEMPLATES.get(template)
    if fn is None:
        raise SystemExit(f"ERROR: 未知模板 {template}，可选：full, {', '.join(TEMPLATES)}")
    return "\n".join(fn(snap))


def main() -> int:
    ap = argparse.ArgumentParser(description="从本地快照渲染九号车辆回答")
    ap.add_argument("--template", "-t", default="status",
                    choices=["status", "full", "battery", "location", "ride",
                             "weather", "params"],
                    help="输出模板（默认 status）")
    ap.add_argument("--max-age", type=int, default=snapstore.DEFAULT_MAX_AGE,
                    help="允许的最陈旧秒数，超过则先刷新")
    ap.add_argument("--refresh", action="store_true", help="强制刷新后再渲染")
    ap.add_argument("--json", action="store_true", help="直接输出快照 JSON")
    ap.add_argument("--path", help="快照路径")
    args = ap.parse_args()

    try:
        if args.refresh:
            snap = snapstore.refresh(args.path)
        else:
            snap, _refreshed, _error = snapstore.ensure(args.max_age, args.path)
    except SystemExit as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(snap, ensure_ascii=False, indent=2))
    else:
        print(render(args.template, snap))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
