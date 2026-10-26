# -*- coding: utf-8 -*-
"""WorkBuddy 每日自动签到（领取每日免费积分）。

    python workbuddy_checkin.py            # 跑一次，打印结果
    pythonw workbuddy_checkin.py --daemon  # 常驻：启动时跑一次，之后每 6 小时检查一次

签到是幂等的：已签就直接跳过，不会重复领取。日志在 logs/checkin.log。
"""
from __future__ import annotations

import datetime
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LOG = ROOT / "logs" / "checkin.log"
LOG.parent.mkdir(parents=True, exist_ok=True)


def log(text: str) -> None:
    line = f"[{datetime.datetime.now():%Y-%m-%d %H:%M:%S}] {text}"
    try:
        with LOG.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass
    if sys.stdout is not None:
        try:
            print(line, flush=True)
        except Exception:  # noqa: BLE001
            pass


def run_once() -> bool:
    try:
        import workbuddy_api as api  # type: ignore

        result = api.ensure_checked_in()
        action = result.get("action")
        streak = result.get("streak_days")
        credit = result.get("today_credit") or result.get("daily_credit")
        total = result.get("total_credits")
        if action == "skip":
            log(f"今天已签到，跳过（连续 {streak} 天，今日 +{credit}，累计 {total}）")
        else:
            log(f"签到成功（连续 {streak} 天，今日 +{credit}，累计 {total}）")
        return True
    except Exception as exc:  # noqa: BLE001
        log(f"签到失败：{exc}")
        return False


def main() -> int:
    if "--daemon" in sys.argv:
        log("签到守夜启动（每 6 小时检查一次）")
        while True:
            ok = run_once()
            time.sleep(6 * 3600 if ok else 1800)
    ok = run_once()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
