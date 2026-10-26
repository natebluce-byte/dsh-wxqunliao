# -*- coding: utf-8 -*-
"""表情包：按回复情绪挑一张本地图片发出去。

图片放在 state/stickers/，文件名用「情绪_名字.png」约定，例如：
    怼_哼.png  无语_就这.png  笑_笑死.png  溜_溜了.png  赞_牛.png
没有文件时，首次运行会用 Pillow 生成一组内置的纯文字表情卡（黄底黑字那种），
保证功能开箱可用。想换成自己的图，直接往目录里丢，文件名前缀决定分类。
"""
from __future__ import annotations

import random
from pathlib import Path
from typing import Iterable

MOODS = ("怼", "无语", "笑", "溜", "赞")

# 关键词 → 情绪
RULES: list[tuple[tuple[str, ...], str]] = [
    (("哼", "才不", "笨蛋", "闭嘴", "少来", "你才", "试试"), "怼"),
    (("无语", "服了", "离谱", "算了", "随便"), "无语"),
    (("哈哈", "笑死", "乐", "绷不住", "憨"), "笑"),
    (("溜了", "睡了", "下了", "告辞", "再见"), "溜"),
    (("牛", "厉害", "强", "可以啊", "不错"), "赞"),
]

BUILTIN = [
    ("怼", "哼", "#FF6B6B"),
    ("怼", "就这？", "#FF8E53"),
    ("无语", "无语", "#9E9E9E"),
    ("笑", "笑死", "#FFD93D"),
    ("溜", "溜了", "#4D96FF"),
    ("赞", "牛", "#6BCB77"),
]


def _font(size: int):
    from PIL import ImageFont

    for name in (r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\simhei.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def ensure_builtin(base: Path) -> None:
    """目录为空时生成内置表情卡。"""
    base.mkdir(parents=True, exist_ok=True)
    if any(base.glob("*.png")):
        return
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return
    for mood, text, color in BUILTIN:
        size = 400
        img = Image.new("RGB", (size, size), color)
        draw = ImageDraw.Draw(img)
        font = _font(120 if len(text) <= 2 else 84)
        box = draw.textbbox((0, 0), text, font=font)
        x = (size - (box[2] - box[0])) / 2 - box[0]
        y = (size - (box[3] - box[1])) / 2 - box[1]
        draw.text((x, y), text, fill="#1a1a1a", font=font)
        draw.rectangle([12, 12, size - 12, size - 12], outline="#1a1a1a", width=6)
        img.save(base / f"{mood}_{text}.png")


class StickerBook:
    def __init__(self, base: Path, chance: float = 0.3, enabled: bool = True) -> None:
        self.base = Path(base)
        self.chance = max(0.0, min(1.0, chance))
        self.enabled = enabled
        if enabled:
            ensure_builtin(self.base)

    def _files(self, mood: str | None) -> list[Path]:
        if not self.base.exists():
            return []
        files = [p for p in self.base.iterdir() if p.suffix.lower() in (".png", ".jpg", ".jpeg", ".gif")]
        if mood:
            hits = [p for p in files if p.name.startswith(mood)]
            if hits:
                return hits
        return files

    def mood_for(self, text: str) -> str | None:
        for words, mood in RULES:
            if any(w in text for w in words):
                return mood
        return None

    def pick(self, reply: str, rng: random.Random | None = None) -> Path | None:
        if not self.enabled or not reply:
            return None
        rng = rng or random
        if rng.random() > self.chance:
            return None
        mood = self.mood_for(reply)
        files = self._files(mood)
        if not files:
            return None
        return rng.choice(files)

    def moods(self) -> Iterable[str]:
        return MOODS
