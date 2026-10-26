# -*- coding: utf-8 -*-
"""下载鲸鱼娘表情包（默认 10 张），转成 PNG 放进 state/stickers/。

素材来源：FeWOO/DeepSeek-meme（MIT）的 assets/default 包
    https://github.com/FeWOO/DeepSeek-meme
该包是「GPT 绘制的鲸鱼娘默认表情」，透明背景、640px。只下载、不修改内容，
转换格式仅为让微信能发（webp → png）。

用法：
    python fetch_stickers.py          # 下载并安装
    python fetch_stickers.py --force  # 重新下载
"""
from __future__ import annotations

import io
import json
import sys
import urllib.request
from pathlib import Path

BASE = "https://raw.githubusercontent.com/FeWOO/DeepSeek-meme/main/assets/default/"
OUT = Path(__file__).resolve().parent / "state" / "stickers"
MARKER = OUT / ".whale-pack.json"
NOTICE = """表情素材：FeWOO/DeepSeek-meme（MIT）assets/default「默认表情」包
来源：https://github.com/FeWOO/DeepSeek-meme
说明：GPT 绘制的鲸鱼娘默认表情，透明背景 640px；本项目仅转成 PNG 以便微信发送。
如不希望使用，删除本目录下的图片即可，程序会回退到内置文字卡。
"""

# 文件名 → 情绪前缀
MOOD_MAP = {
    "01-received": "赞",
    "02-thanks": "笑",
    "03-im-here": "笑",
    "04-nice-work": "赞",
    "05-good-night": "溜",
    "06-good-morning": "笑",
    "07-milk-tea": "笑",
    "08-huh": "无语",
    "09-hmph": "怼",
    "10-sleepy": "溜",
}

BUILTIN = ["怼_哼.png", "怼_就这？.png", "无语_无语.png", "笑_笑死.png", "溜_溜了.png", "赞_牛.png"]


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "dsh-wxqunliao/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def main() -> int:
    force = "--force" in sys.argv
    OUT.mkdir(parents=True, exist_ok=True)
    if MARKER.exists() and not force:
        print(f"already installed: {MARKER}")
        return 0

    from PIL import Image

    manifest = json.loads(fetch(BASE + "manifest.json").decode("utf-8"))
    installed = 0
    for item in manifest.get("stickers", []):
        stem = Path(item["file"]).stem
        mood = MOOD_MAP.get(stem, "笑")
        name = item.get("name") or stem
        safe = "".join(ch for ch in name if ch not in '\\/:*?"<>|').strip() or stem
        img = Image.open(io.BytesIO(fetch(BASE + item["file"]))).convert("RGBA")
        target = OUT / f"{mood}_{safe}.png"
        img.save(target)
        installed += 1
        print(f"  + {target.name}")

    # 有真图之后删掉内置文字卡，避免风格混杂
    for name in BUILTIN:
        p = OUT / name
        if p.exists():
            p.unlink()

    MARKER.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT / "NOTICE.txt").write_text(NOTICE, encoding="utf-8")
    print(f"done: {installed} stickers -> {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
