#!/usr/bin/env python3
"""One local snapshot of every Ninebot figure we can read.

Why this file exists
--------------------
A live vehicle read costs network round-trips: vehicle ~1.3 s, plus geo,
weather and travel. That is fine once a day; it is wasteful on every
question. So one collector writes everything it fetched into a single
JSON file, and every reader — the fast card templates, ad-hoc parameter
lookups, the daily report — reads that file instead of hitting the API
again.

Freshness is explicit, never implied. The snapshot carries `fetched_at`
and `epoch`; callers pass `--max-age` and decide how stale is too stale.
A failed refresh never destroys the previous snapshot: old data plus a
visible `stale`/`error` marker beats no answer at all, and beats a fresh
answer even less than that.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any, Dict, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

def _default_state_dir() -> str:
    """Resolve the state dir without trusting $HOME to exist.

    `expanduser("~/.hermes/...")` leaves the tilde untouched when HOME is
    missing (cron's minimal env), which would create a literal `~`
    directory under the CWD. `expanduser("~")` falls back to the passwd
    entry, so this stays correct in both contexts.
    """
    explicit = os.environ.get("NINEBOT_STATE_DIR")
    if explicit:
        return os.path.expanduser(explicit)
    return os.path.join(os.path.expanduser("~"), ".hermes", "state", "ninebot")


DEFAULT_STATE_DIR = _default_state_dir()
DEFAULT_PATH = os.environ.get(
    "NINEBOT_SNAPSHOT", os.path.join(DEFAULT_STATE_DIR, "snapshot.json"))
DEFAULT_MAX_AGE = int(os.environ.get("NINEBOT_SNAPSHOT_MAX_AGE", "600"))


def snapshot_path(path: Optional[str] = None) -> str:
    return path or DEFAULT_PATH


def load(path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Read the last snapshot, or None when there is none / it is broken."""
    try:
        with open(snapshot_path(path), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def save(snap: Dict[str, Any], path: Optional[str] = None) -> str:
    """Atomic write: a half-written snapshot must never be readable."""
    dest = snapshot_path(path)
    os.makedirs(os.path.dirname(dest), mode=0o700, exist_ok=True)
    tmp = dest + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(snap, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, dest)
    return dest


def refresh(path: Optional[str] = None) -> Dict[str, Any]:
    """Fetch live and persist. Raises on any failure to reach the vehicle."""
    import ninebot_report as report
    snap = report.collect_snapshot()
    save(snap, path)
    return snap


def age_seconds(snap: Dict[str, Any]) -> float:
    try:
        return max(0.0, time.time() - float(snap.get("epoch") or 0))
    except (TypeError, ValueError):
        return float("inf")


def age_text(seconds: float) -> str:
    if seconds < 90:
        return "刚刚"
    if seconds < 3600:
        return f"{int(seconds // 60)} 分钟前"
    if seconds < 86400:
        return f"{seconds / 3600:.1f} 小时前"
    return f"{int(seconds // 86400)} 天前"


def ensure(max_age: int = DEFAULT_MAX_AGE,
           path: Optional[str] = None) -> Tuple[Dict[str, Any], bool, Optional[str]]:
    """Return (snapshot, refreshed, error).

    `error` is set when the refresh failed but an older snapshot was
    usable — the caller still gets data, with the staleness marked.
    """
    old = load(path)
    if old is not None and age_seconds(old) <= max_age:
        return old, False, None
    try:
        return refresh(path), True, None
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - degrade to the last snapshot
        if old is not None:
            stale = dict(old)
            stale["stale"] = True
            stale["error"] = f"{type(exc).__name__}: {exc}"
            return stale, False, stale["error"]
        raise SystemExit(f"ERROR: 快照不可用且刷新失败: {type(exc).__name__}: {exc}")


def summary(snap: Dict[str, Any]) -> str:
    status = snap.get("status") or {}
    name = (snap.get("device") or {}).get("name") or "九号"
    return (f"{name} 电量 {status.get('battery', '—')}% "
            f"@ {snap.get('fetched_at', '—')} "
            f"({age_text(age_seconds(snap))})")


def main() -> int:
    ap = argparse.ArgumentParser(description="九号车辆本地快照")
    ap.add_argument("--refresh", action="store_true", help="强制重新抓取")
    ap.add_argument("--max-age", type=int, default=DEFAULT_MAX_AGE,
                    help=f"可接受的最大陈旧秒数（默认 {DEFAULT_MAX_AGE}）")
    ap.add_argument("--path", help="快照路径（默认 ~/.hermes/state/ninebot/snapshot.json）")
    ap.add_argument("--summary", action="store_true", help="只打印一行摘要")
    ap.add_argument("--quiet", action="store_true", help="成功时不输出")
    args = ap.parse_args()

    try:
        if args.refresh:
            snap, refreshed, error = refresh(args.path), True, None
        else:
            snap, refreshed, error = ensure(args.max_age, args.path)
    except SystemExit as exc:  # message already phrased for the caller
        print(str(exc), file=sys.stderr)
        return 1

    if error:
        print(f"WARN: 快照刷新失败，使用 {age_text(age_seconds(snap))} 的数据（{error}）",
              file=sys.stderr)

    if not args.quiet:
        if args.summary:
            print(summary(snap))
        else:
            json.dump(snap, sys.stdout, ensure_ascii=False, indent=2)
            sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
