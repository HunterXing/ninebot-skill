#!/usr/bin/env python3
"""Render the daily vehicle report.

Feishu delivers plain Markdown text, so there are no cards to build —
the hierarchy has to come from bold text, indentation, alignment and
whitespace instead. The layout follows one rule: the number that matters
most is the one that reads first.

Sections, in reading order:
    headline  → battery → range → location → state → weather → riding

Every figure is either measured or explicitly labelled as derived. When a
value is unavailable the report says so and why — a silent omission reads
as "nothing to report", which is a different thing entirely.
"""
from __future__ import annotations

import datetime
from typing import Any, Dict, List, Optional

import ninebot_weather as weather

BLOCK_FULL = "█"
BLOCK_EMPTY = "░"


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
    return BLOCK_FULL * filled + BLOCK_EMPTY * (width - filled)


def _charge_emoji(state: str) -> str:
    return {
        "charging": "🔌 充电中",
        "charge": "🔌 充电中",
        "full": "✅ 已充满",
        "not_charge": "⚪ 未充电",
    }.get(state, "⚪ " + (state or "未知"))


def _power_emoji(state: str) -> str:
    return {"ON": "🟢 已开机", "OFF": "⚫ 已关机"}.get(state, "⚪ " + (state or "未知"))


def _lock_text(lock: Any) -> str:
    """The steering-wheel lock only.

    Seat-trunk / barrel lock is not shown: this vehicle has no seat
    bucket, and the field always reports "unlocked", which reads as a
    fault that does not exist.
    """
    return "🔒 已锁车" if str(lock) in ("1", "True") else "🔓 未锁车"


# Precipitation below this (mm, 24 h total) is dew-level drizzle that the
# provider reports even on an overcast day — it must not trigger a
# "bring a raincoat" reminder.
RAIN_MM_THRESHOLD = 1.0


def _has_precip(precip: Any) -> bool:
    """True when the forecast carries meaningful precipitation."""
    try:
        return float(precip) >= RAIN_MM_THRESHOLD
    except (TypeError, ValueError):
        return False


def _reminders(status: Dict[str, Any], rainy: bool) -> List[str]:
    """The two things worth saying out loud.

    Rain matters for the ride; a parked vehicle matters for the seat and
    the electronics. "Parked" is read as powered off — a vehicle that is
    still on is being ridden or about to be.
    """
    tips: List[str] = []
    power = str(status.get("powerStatus") or status.get("status") or "").upper()
    parked = power == "OFF"
    if parked:
        # A parked bike is the one that needs covering; rain makes it urgent
        # (seat, dash and connectors take the weather), dry weather makes it
        # routine (sun, dust).
        if rainy:
            tips.append("今天有雨，车辆停在户外，记得遮盖车衣或放进车库")
        else:
            tips.append("车辆已停放，遮盖车衣或放进车库")
    elif rainy:
        tips.append("今天有雨，骑行记得带雨衣")
    return tips


def render(status: Dict[str, Any],
           geo: Optional[Dict[str, Any]] = None,
           wx: Optional[Dict[str, Any]] = None,
           travel: Optional[Dict[str, Any]] = None,
           mileage: Optional[Dict[str, Any]] = None,
           now: Optional[datetime.datetime] = None) -> str:
    """Build the whole report as Markdown text."""
    now = now or datetime.datetime.now()
    travel = travel or {}
    lines: List[str] = []

    name = status.get("device_name") or "九号电自 Fz1 60"
    weekday = weather.weekday_of(now.strftime("%Y-%m-%d"))
    stamp = now.strftime("%Y-%m-%d")

    # ---- headline -------------------------------------------------------
    lines.append(f"**🛵 {name} · 车辆日报**")
    lines.append(f"📅 {stamp} {weekday}　·　更新于 {now.strftime('%H:%M')}")
    lines.append("")

    # ---- battery --------------------------------------------------------
    battery = _num(status.get("battery"))
    lines.append("**🔋 电量**")
    if battery is not None:
        lines.append(f"{_battery_bar(battery)}　**{battery:.0f}%**")
    bits = []
    if status.get("voltage") is not None:
        bits.append(f"电压 {_fmt(status.get('voltage'), 'V', 1)}")
    if status.get("temperature") is not None:
        bits.append(f"电池 {_fmt(status.get('temperature'), '°C', 0)}")
    if status.get("cycles") is not None and status.get("hasCycleSupport", True):
        bits.append(f"循环 {status.get('cycles')} 次")
    if bits:
        lines.append("　".join(bits))
    lines.append("")

    # ---- range ----------------------------------------------------------
    est = _num(status.get("estimateMileage"))
    precise = _num(status.get("preciseEstimateMileage"))
    ai = _num(status.get("aiEstimateMileage"))
    lines.append("**🛣 续航**")
    row = []
    if precise is not None:
        row.append(f"实景 **{precise:.1f} km**")
    if est is not None:
        row.append(f"标准工况 {est:.1f} km")
    if row:
        lines.append("　｜　".join(row))
    # Three different numbers need three different names, or the reader is
    # left guessing which one to trust.
    if ai is not None:
        lines.append(f"　按你的骑行习惯估算 {ai:.1f} km")
        if precise and ai < precise * 0.8:
            lines.append("　（短途多、启停频繁，所以比实景值低）")
    if precise is not None and battery:
        full = precise / (battery / 100.0)
        lines.append(f"　满电理论 ≈ {full:.0f} km（{precise:.1f} ÷ {battery:.0f}% 反推）")
    lines.append("")

    # ---- location -------------------------------------------------------
    lines.append("**📍 位置**")
    address = status.get("location") or ""
    detail = status.get("locationDetail") or ""
    house = ""
    if geo and geo.get("houseNumber") and geo.get("street"):
        house = f"{geo['street']}{geo['houseNumber']}"
    lines.append(address or detail or "位置未知")
    extras = []
    if house and house not in address:
        extras.append(f"门牌 {house}")
    if geo and geo.get("poiDistance") is not None:
        extras.append(f"最近 POI {geo['poiDistance']} m")
    if status.get("lock") is not None:
        extras.append(_lock_text(status.get("lock")))
    if extras:
        lines.append("　｜　".join(extras))
    lines.append("")

    # ---- mileage --------------------------------------------------------
    mileage = mileage or {}
    if mileage:
        lines.append("**📏 里程**")
        cells = []
        day = mileage.get("day") or {}
        week = mileage.get("week") or {}
        month = mileage.get("month") or {}
        year = mileage.get("year") or {}
        total = mileage.get("all") or {}
        cells.append(f"今日 **{day.get('km', 0):.1f} km**")
        cells.append(f"本周 {week.get('km', 0):.1f} km（{week.get('times', 0)} 次）")
        cells.append(f"本月 {month.get('km', 0):.1f} km（{month.get('times', 0)} 次）")
        lines.append("　｜　".join(cells))
        cells2 = [f"今年 {year.get('km', 0):.1f} km（{year.get('times', 0)} 次）"]
        if total:
            since = str(total.get("since") or "")
            label = f"自 {since[:4]}-{since[4:6]}" if len(since) == 6 else ""
            cells2.append(f"累计 {total.get('km', 0):.1f} km" + (f"（{label}）" if label else ""))
        lines.append("　｜　".join(cells2))
        if not mileage.get("odometerAvailable", True):
            lines.append("　整车总里程（ODO）：共享账号不提供，接口返回空")
        lines.append("")

    # ---- state ----------------------------------------------------------
    lines.append("**⚡ 状态**")
    state_bits = [
        _power_emoji(str(status.get("powerStatus") or status.get("status") or "")),
        _charge_emoji(str(status.get("chargingState") or "")),
    ]
    if status.get("smartServiceExpired") is not None:
        expired = str(status.get("smartServiceExpired")) in ("1", "True")
        state_bits.append("🛡 智能服务已过期" if expired else "🛡 智能服务有效")
    lines.append("　｜　".join(state_bits))
    lines.append("")

    # ---- weather --------------------------------------------------------
    rainy = False
    if wx and wx.get("days"):
        city = (wx.get("city") or {}).get("name") or ""
        cur = wx.get("now") or {}
        if cur:
            lines.append("**🌡 实时**")
            parts = [f"{cur.get('text', '')} {cur.get('temp', '—')}°C"]
            if cur.get("humidity"):
                parts.append(f"湿度 {cur['humidity']}%")
            if cur.get("windDir"):
                parts.append(f"{cur['windDir']} {cur.get('windScale', '')}级")
            lines.append("　｜　".join(parts))

        # Only today — the report goes out at 07:45, so a two-day-out
        # forecast is noise.
        today = wx["days"][0]
        lo, hi = today.get("tempMin"), today.get("tempMax")
        span = f"{lo}~{hi}°C" if lo and hi else "—"
        bits = [span]
        if today.get("textDay"):
            bits.insert(0, today["textDay"])
        if today.get("windScaleDay"):
            bits.append(f"{today.get('windDirDay', '')} {today['windScaleDay']}级")
        lines.append(f"**☁ 今天**　{'　｜　'.join(bits)}")
        if city:
            lines.append(f"　天气源：高德 · {city}")

        text_today = f"{today.get('textDay') or ''}{today.get('textNight') or ''}"
        # Amap reports no precipitation amount, so the forecast text is
        # the only signal — and it is the reliable one anyway.
        rainy = ("雨" in text_today) or ("雪" in text_today)
        lines.append("")

    # ---- reminders ------------------------------------------------------
    tips = _reminders(status, rainy)
    if tips:
        lines.append("**💡 提醒**")
        for tip in tips:
            lines.append(f"　· {tip}")
        lines.append("")

    # ---- riding ---------------------------------------------------------
    lines.append("**🏍 骑行记录**")
    rides = list(travel.get("list") or [])
    times = travel.get("times") or 0
    if times and rides:
        # The gateway does NOT return rides in chronological order, so the
        # newest one has to be found rather than taken from index 0.
        def _stamp(r: Dict[str, Any]) -> int:
            try:
                return int(r.get("end_time") or r.get("start_time") or 0)
            except (TypeError, ValueError):
                return 0
        rides.sort(key=_stamp, reverse=True)

        total_km = float(travel.get("total_mileages") or 0)
        secs = int(travel.get("duration") or 0)
        # `ec` is energy in Wh, not a percentage — 120 Wh over 8.6 km is
        # ~14 Wh/km, which is normal for a 48 V lead-acid e-bike.
        ec = travel.get("ec") or 0
        # Count and distance already appear in the mileage block, so this
        # block only adds what that one does not carry.
        head = []
        if secs:
            head.append(f"本月骑行时长 {secs // 60}分{secs % 60}秒")
        if ec:
            head.append(f"能耗 {ec} Wh")
        if head:
            lines.append("　｜　".join(head))

        # Per-day totals, aggregated from the rides themselves — the
        # gateway's `detail[]` array is a fixed-length day grid whose
        # index-to-date mapping is not documented, so deriving it from
        # the ride timestamps is the reliable route.
        per_day: Dict[str, float] = {}
        for r in rides:
            stamp = str(r.get("end_time_format") or "")
            day = stamp[:10] if len(stamp) >= 10 else ""
            if not day:
                continue
            per_day[day] = per_day.get(day, 0.0) + float(r.get("mileages") or 0)
        if per_day:
            ordered = sorted(per_day.items())[-7:]
            lines.append("　逐日里程：" + "　".join(
                f"{d[5:]} {v:.1f}km" for d, v in ordered))

        last = rides[0]
        lines.append(f"**最近一次**　{last.get('end_time_format') or ''}")
        bits = [f"{float(last.get('mileages') or 0):.2f} km",
                f"{int(last.get('duration') or 0)}秒",
                f"均速 {last.get('speed')} km/h"]
        if last.get("used_electricity") is not None:
            bits.append(f"耗电 {last.get('used_electricity')}%")
        lines.append("　" + "　｜　".join(bits))

        if len(rides) > 1:
            lines.append("　其余骑行：")
            for r in rides[1:6]:
                stamp = str(r.get("end_time_format") or "")
                lines.append(f"　　{stamp[5:16] or stamp}　{r.get('mileages')} km　"
                             f"{int(r.get('duration') or 0)}秒　均速 {r.get('speed')}")
    elif travel.get("error"):
        lines.append(f"取数失败：{travel['error']}")
    else:
        lines.append(f"本月（{travel.get('month') or ''}）无骑行记录")
    lines.append("")
    lines.append("———")
    lines.append("数据源：九号车辆接口 + 高德（逆地理编码 · 天气）")
    return "\n".join(lines)


def collect_snapshot() -> Dict[str, Any]:
    """Fetch every figure once and return the data, not the rendering.

    This is the only place that touches the network for a vehicle read.
    The live report and the local snapshot cache both build on it, so a
    field added here reaches every consumer without a second fetch path.
    """
    import os
    import sys

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import ninebot_query as nq

    cfg = nq.ACCOUNT_CONFIG
    backend = nq.pick_backend(None, cfg)
    if backend != "account":
        raise RuntimeError("当前是官方后端，本日报只支持账号后端")
    session = nq.load_session()
    if not session:
        raise RuntimeError("九号会话文件缺失或已失效")
    token = session.get("access_token", "")
    devices = nq.account_list_devices(cfg, token, "zh")
    if not devices:
        raise RuntimeError("九号车辆列表为空（session 失效或车辆解绑）")
    dev = devices[0]
    sn = str(dev.get("sn") or "")
    status = nq.account_device_info(cfg, token, sn)
    status.setdefault("device_name", dev.get("name", ""))
    # Normalise the raw upstream flags (pwr=1, charging=0) into the
    # words the report prints.
    status["powerStatus"] = nq.normalize_power(status.get("status"))
    status["chargingState"] = nq.normalize_charging(status.get("chargingState"))

    # Voltage / temperature / cycle count live on the battery endpoint.
    battery: Dict[str, Any] = {}
    try:
        battery = nq.account_battery_info(cfg, token, sn)
        for key in ("voltage", "temperature", "cycles", "health"):
            if battery.get(key) is not None:
                status[key] = battery[key]
    except Exception:  # noqa: BLE001 - the report is still useful without them
        battery = {}

    lat, lon = status.get("lat"), status.get("lon")

    travel: Dict[str, Any] = {}
    mileage: Dict[str, Any] = {}
    try:
        travel = nq.account_travel_list(cfg, token, sn)
    except Exception:  # noqa: BLE001 - the report is still useful without it
        travel = {}
    try:
        mileage = nq.travel_mileage_summary(
            cfg, token, sn, travel.get("month") or "")
    except Exception:  # noqa: BLE001 - mileage is a bonus, not a dependency
        mileage = {}

    geo = None
    try:
        import ninebot_geo as geo_mod
        geo = geo_mod.reverse_geocode(lat, lon)
    except Exception:  # noqa: BLE001 - weather/geo must never kill the report
        geo = None

    # The address lookup already returned the adcode the weather endpoint
    # keys off, so hand it over instead of resolving the city again.
    try:
        wx = weather.collect(lat, lon,
                             adcode=(geo or {}).get("adcode"),
                             city_name=(geo or {}).get("district", ""))
    except Exception:  # noqa: BLE001 - weather is a bonus, not a dependency
        wx = {}

    fetched = datetime.datetime.now().astimezone()
    return {
        "fetched_at": fetched.isoformat(timespec="seconds"),
        "epoch": int(fetched.timestamp()),
        "device": {"name": status.get("device_name", ""), "sn": sn},
        "status": status,
        "battery": battery,
        "geo": geo or {},
        "weather": wx,
        "travel": travel,
        "mileage": mileage,
    }


def build_live() -> str:
    """Pull everything and render it — the mode the cron job uses."""
    snap = collect_snapshot()
    return render(snap["status"], geo=snap.get("geo"), wx=snap.get("weather"),
                  travel=snap.get("travel"), mileage=snap.get("mileage"))


if __name__ == "__main__":
    import argparse
    import json
    import sys

    ap = argparse.ArgumentParser(description="渲染车辆日报")
    ap.add_argument("--status", help="status JSON file")
    ap.add_argument("--live", action="store_true",
                    help="fetch the vehicle state and render it")
    args = ap.parse_args()

    if args.live:
        try:
            print(build_live())
        except Exception as exc:  # noqa: BLE001
            print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
            raise SystemExit(1)
    elif args.status:
        with open(args.status, encoding="utf-8") as fh:
            print(render(json.load(fh)))
    else:
        print("需要 --live 或 --status", file=sys.stderr)
        raise SystemExit(2)
