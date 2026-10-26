# -*- coding: utf-8 -*-
"""无窗口守夜进程：用 pythonw.exe 跑 bot.py，退出后自动重拉。

由启动文件夹里的快捷方式在登录时启动，也可以手动：
    pythonw.exe service.pyw

没有控制台窗口，日志在 logs/bot-*.log 与 logs/service.log。
"""
from __future__ import annotations

import datetime
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LOG_DIR = ROOT / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
SERVICE_LOG = LOG_DIR / "service.log"
BOT = ROOT / "bot.py"
# pythonw.exe 与 python.exe 同目录；没有就退回 python.exe（会有黑框）
PYW = Path(sys.executable).with_name("pythonw.exe")
PY = PYW if PYW.exists() else Path(sys.executable)
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def log(text: str) -> None:
    line = f"[{datetime.datetime.now():%Y-%m-%d %H:%M:%S}] {text}\n"
    try:
        with SERVICE_LOG.open("a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError:
        pass


def main() -> None:
    log(f"watchdog start (python={PY.name})")
    while True:
        log("starting bot.py")
        started = time.time()
        try:
            proc = subprocess.run(
                [str(PY), str(BOT), "run"],
                cwd=str(ROOT),
                creationflags=CREATE_NO_WINDOW,
            )
            code = proc.returncode
        except Exception as exc:  # noqa: BLE001
            code = f"exception: {exc}"
        ran = time.time() - started
        if ran < 120:
            log(f"bot exited ({code}) after {ran:.0f}s - likely startup error, retry in 60s")
            time.sleep(60)
        else:
            log(f"bot exited ({code}) after {ran:.0f}s - retry in 10s")
            time.sleep(10)


if __name__ == "__main__":
    main()
