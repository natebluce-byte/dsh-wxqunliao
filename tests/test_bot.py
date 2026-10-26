# -*- coding: utf-8 -*-
"""纯逻辑单测：不碰微信、不调模型，跑得快。

    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import os
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot  # noqa: E402


class TestParsing(unittest.TestCase):
    def test_group_body_strips_sender_prefix(self):
        self.assertEqual(
            bot.clean_group_body("wxid_abc123:\n@小助手 你好"),
            "@小助手 你好",
        )

    def test_group_body_keeps_normal_text(self):
        text = "这周末去爬山吗？"
        self.assertEqual(bot.clean_group_body(text), text)

    def test_group_body_keeps_multiline_without_prefix(self):
        text = "第一行\n第二行"
        self.assertEqual(bot.clean_group_body(text), text)

    def test_trigger_by_mention(self):
        self.assertTrue(bot.is_triggered("@小助手 在吗", ["小助手"], ["/ai"]))

    def test_trigger_by_prefix(self):
        self.assertTrue(bot.is_triggered("/ai 今天几号", ["小助手"], ["/ai"]))

    def test_no_trigger(self):
        self.assertFalse(bot.is_triggered("今天天气不错", ["小助手"], ["/ai"]))

    def test_strip_mentions(self):
        self.assertEqual(
            bot.strip_mentions("@小助手 @别人 帮我看下", ["小助手"]),
            "@别人 帮我看下",
        )


class TestSanitize(unittest.TestCase):
    def test_removes_markdown(self):
        raw = "## 标题\n**重点**和`代码`\n\n\n尾巴"
        out = bot.sanitize_reply(raw, 100)
        self.assertNotIn("**", out)
        self.assertNotIn("##", out)
        self.assertNotIn("`", out)
        self.assertIn("重点", out)

    def test_truncates(self):
        out = bot.sanitize_reply("字" * 50, 10)
        self.assertLessEqual(len(out), 10)
        self.assertTrue(out.endswith("…"))

    def test_strips_whitespace(self):
        self.assertEqual(bot.sanitize_reply("  你好  ", 10), "你好")


class TestRateLimiter(unittest.TestCase):
    def _cfg(self, cooldown=0, per_group=100, global_limit=100):
        return {"limits": {
            "per_group_cooldown_sec": cooldown,
            "per_group_per_hour": per_group,
            "global_per_hour": global_limit,
        }}

    def test_cooldown_blocks_second(self):
        rl = bot.RateLimiter(self._cfg(cooldown=15))
        self.assertTrue(rl.allow("g1")[0])
        ok, reason = rl.allow("g1")
        self.assertFalse(ok)
        self.assertIn("冷却", reason)

    def test_other_group_not_affected(self):
        rl = bot.RateLimiter(self._cfg(cooldown=15))
        rl.allow("g1")
        self.assertTrue(rl.allow("g2")[0])

    def test_per_group_hourly_cap(self):
        rl = bot.RateLimiter(self._cfg(cooldown=0, per_group=2))
        self.assertTrue(rl.allow("g1")[0])
        self.assertTrue(rl.allow("g1")[0])
        ok, reason = rl.allow("g1")
        self.assertFalse(ok)
        self.assertIn("额度", reason)

    def test_global_cap(self):
        rl = bot.RateLimiter(self._cfg(cooldown=0, per_group=100, global_limit=2))
        self.assertTrue(rl.allow("g1")[0])
        self.assertTrue(rl.allow("g2")[0])
        ok, reason = rl.allow("g3")
        self.assertFalse(ok)
        self.assertIn("全局", reason)


class TestPrompt(unittest.TestCase):
    def test_template_fills(self):
        text = bot.PROMPT_TEMPLATE.format(
            persona="你是测试人设。",
            group="测试群",
            context="小明：在吗",
            ask="@小助手 你好",
            max_chars=600,
        )
        self.assertIn("测试群", text)
        self.assertIn("测试人设", text)
        self.assertIn("小明：在吗", text)
        self.assertIn("@小助手 你好", text)
        self.assertIn("600", text)

    def test_default_persona_used_when_empty(self):
        cfg = bot.default_config()
        cfg["bot"]["persona"] = ""
        b = bot.Bot(cfg, dry_run=True)
        self.assertEqual(b.persona, bot.DEFAULT_PERSONA)


class TestApiKey(unittest.TestCase):
    def test_env_wins(self):
        old = os.environ.get("DEEPSEEK_API_KEY")
        os.environ["DEEPSEEK_API_KEY"] = "sk-env-test"
        try:
            self.assertEqual(bot.resolve_api_key({}), "sk-env-test")
        finally:
            if old is None:
                os.environ.pop("DEEPSEEK_API_KEY", None)
            else:
                os.environ["DEEPSEEK_API_KEY"] = old

    def test_missing_returns_none(self):
        old = os.environ.pop("DEEPSEEK_API_KEY", None)
        home = os.environ.get("USERPROFILE")
        os.environ["USERPROFILE"] = str(Path("__nonexistent_home__"))
        try:
            self.assertIsNone(bot.resolve_api_key({}))
        finally:
            os.environ["USERPROFILE"] = home or ""
            if old is not None:
                os.environ["DEEPSEEK_API_KEY"] = old


class TestConfig(unittest.TestCase):
    def test_defaults_have_required_keys(self):
        cfg = bot.default_config()
        for key in ("groups", "bot", "limits", "backend", "wechat"):
            self.assertIn(key, cfg)
        self.assertIn("dsh_cmd", cfg["backend"])
        self.assertIn("work_dir", cfg["backend"])

    def test_nested(self):
        cfg = {"a": {"b": {"c": 1}}}
        self.assertEqual(bot.nested(cfg, "a.b.c"), 1)
        self.assertEqual(bot.nested(cfg, "a.b.x", "fallback"), "fallback")
        self.assertIsNone(bot.nested(cfg, "z.y"))


if __name__ == "__main__":
    unittest.main()
