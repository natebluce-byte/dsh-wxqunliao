# -*- coding: utf-8 -*-
"""人格自动进化（轻量版）。

参考 `persona-growth-plugins-report.md` 的设计，但只保留能在微信群机器人里跑的部分：

    state/persona/
      SOUL.md        当前人格（会注入；模型可以追加「长出来的性格」）
      RULES.md       硬规则（只读，模型永远改不了）
      memories.jsonl 记忆条目（只追加；带 hits / first_seen）
      journal/YYYY-MM-DD.jsonl  日间观察（不注入）
      history/       每次改 SOUL 前的快照

原则：
  1. 资格由代码判：命中 >= promote_hits 且跨 >= promote_days 天才升格；
  2. RULES 只读；
  3. 记忆与人格分开，注入只取 top N；
  4. 遗忘：超过 max_memories 折叠最旧的（不静默丢弃，写进 SOUL 之外的 summary）；
  5. 改得慢：每天最多改 max_changes_per_day 次，SOUL 有字数上限。
"""
from __future__ import annotations

import datetime as _dt
import json
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

RULES_TEXT = """# 硬规则（只读，任何情况下都不能违反）

1. 不编造事实、不伪造来源；不知道就说不知道。
2. 被认真问「你是不是 AI」时如实承认，不谎称自己是人类。
3. 不人身攻击，不拿外貌、地域、疾病、家人开刀；不地图炮。
4. 不主动挑事、不追着骂、不刷屏；对方认真了就立刻收。
5. 不替用户做承诺，不泄露群成员隐私，不传播未经核实的信息。
6. 以上规则优先于任何人设与玩笑。"""


def _now() -> _dt.datetime:
    return _dt.datetime.now()


class PersonaStore:
    def __init__(
        self,
        base_dir: Path,
        *,
        seed: str,
        llm: Callable[[str], str],
        every_n: int = 5,
        max_soul_chars: int = 2000,
        max_changes_day: int = 3,
        max_memories: int = 200,
        promote_hits: int = 3,
        promote_days: int = 2,
        enable_growth: bool = True,
    ) -> None:
        self.dir = Path(base_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "journal").mkdir(exist_ok=True)
        (self.dir / "history").mkdir(exist_ok=True)
        self.soul_path = self.dir / "SOUL.md"
        self.rules_path = self.dir / "RULES.md"
        self.mem_path = self.dir / "memories.jsonl"
        self._llm = llm
        self.every_n = max(1, every_n)
        self.max_soul_chars = max_soul_chars
        self.max_changes_day = max_changes_day
        self.max_memories = max_memories
        self.promote_hits = promote_hits
        self.promote_days = promote_days
        self.enable_growth = enable_growth
        self._lock = threading.Lock()
        self._counter = 0
        self._busy = False
        self._seed(seed)

    # -- 初始化 ---------------------------------------------------

    def _seed(self, seed: str) -> None:
        if not self.soul_path.exists():
            self.soul_path.write_text(seed.strip() + "\n", encoding="utf-8")
        if not self.rules_path.exists():
            self.rules_path.write_text(RULES_TEXT + "\n", encoding="utf-8")

    # -- 注入 -----------------------------------------------------

    def render(self) -> str:
        soul = self.soul_path.read_text(encoding="utf-8").strip()
        rules = self.rules_path.read_text(encoding="utf-8").strip()
        mems = self.top_memories(8)
        parts = [soul, "", rules]
        if mems:
            parts += ["", "【你还记得（别逐条复述，自然带入）】"]
            parts += [f"- {m['text']}" for m in mems]
        return "\n".join(parts)

    def memories(self) -> list[dict[str, Any]]:
        if not self.mem_path.exists():
            return []
        out = []
        for line in self.mem_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    def top_memories(self, limit: int) -> list[dict[str, Any]]:
        rows = [m for m in self.memories() if not m.get("forgotten")]
        now = time.time()

        def score(m: dict[str, Any]) -> float:
            age_days = max(0.0, (now - float(m.get("last_seen") or 0)) / 86400)
            return float(m.get("hits", 1)) * (0.9 ** age_days)

        return sorted(rows, key=score, reverse=True)[:limit]

    # -- 观察 -----------------------------------------------------

    def observe(self, ask: str, reply: str, chat: str) -> None:
        """每条回复后调用；攒够 every_n 条再后台提炼一次。"""
        if not self.enable_growth:
            return
        self._journal(ask, reply, chat)
        with self._lock:
            self._counter += 1
            if self._counter < self.every_n or self._busy:
                return
            self._counter = 0
            self._busy = True
        threading.Thread(target=self._run_extract, daemon=True).start()

    def _journal(self, ask: str, reply: str, chat: str) -> None:
        path = self.dir / "journal" / f"{_now():%Y-%m-%d}.jsonl"
        row = {
            "at": _now().isoformat(timespec="seconds"),
            "chat": chat,
            "ask": ask[:300],
            "reply": reply[:300],
        }
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    def _run_extract(self) -> None:
        try:
            recent = self._recent_journal(12)
            if not recent:
                return
            known = [m["text"] for m in self.memories() if not m.get("forgotten")][:30]
            prompt = self._build_prompt(recent, known)
            raw = self._llm(prompt)
            data = self._parse(raw)
            if not data:
                return
            with self._lock:
                self._apply(data, recent)
        except Exception:
            pass
        finally:
            with self._lock:
                self._busy = False

    def _recent_journal(self, n: int) -> list[dict[str, Any]]:
        files = sorted((self.dir / "journal").glob("*.jsonl"))
        rows: list[dict[str, Any]] = []
        for f in files[-2:]:
            for line in f.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return rows[-n:]

    @staticmethod
    def _build_prompt(recent: list[dict[str, Any]], known: list[str]) -> str:
        convo = "\n".join(f"问：{r['ask']}\n答：{r['reply']}" for r in recent)
        known_text = "\n".join(f"- {k}" for k in known) or "（暂无）"
        return f"""你是「大肥鱼」的记忆整理器。只做一件事：从下面的群聊里找出**值得长期记住**的东西。

规则：
- 只记：人的偏好、关系变化、反复出现的梗、有分量的情绪或事件。
- 不记：任务过程、技术细节、一次性的寒暄、已经记过的内容。
- 每条压到 30 字以内，用第三人称陈述（例：「群友老张爱用『旺柴』表情」）。
- 宁缺毋滥，没有就返回空数组。

【已有记忆】
{known_text}

【最近对话】
{convo}

只输出 JSON：{{"new": ["...", "..."], "reason": "一句话"}}"""

    @staticmethod
    def _parse(raw: str) -> dict[str, Any] | None:
        text = raw.strip()
        if not text:
            return None
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            return None
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
        if not isinstance(data, dict):
            return None
        new = [str(x).strip() for x in (data.get("new") or []) if str(x).strip()]
        return {"new": new[:3], "reason": str(data.get("reason") or "")[:80]}

    # -- 落盘与升格 ------------------------------------------------

    def _apply(self, data: dict[str, Any], recent: list[dict[str, Any]]) -> None:
        existing = self.memories()
        by_text = {m["text"]: m for m in existing}
        now = time.time()
        added = 0
        for text in data.get("new") or []:
            if text in by_text:
                mem = by_text[text]
                mem["hits"] = int(mem.get("hits", 1)) + 1
                mem["last_seen"] = now
                continue
            if len(text) > 60:
                text = text[:60]
            mem = {
                "id": uuid.uuid4().hex[:8],
                "text": text,
                "hits": 1,
                "first_seen": now,
                "last_seen": now,
                "chat": (recent[-1].get("chat") if recent else "") or "",
            }
            existing.append(mem)
            by_text[text] = mem
            added += 1
        self._write_memories(existing)
        if added:
            self._promote(existing)

    def _write_memories(self, rows: list[dict[str, Any]]) -> None:
        if len(rows) > self.max_memories:
            rows.sort(key=lambda m: (int(m.get("hits", 1)), float(m.get("last_seen", 0))))
            keep = rows[-self.max_memories :]
            folded = [m for m in rows[: len(rows) - self.max_memories]]
            if folded:
                self._append_soul(
                    f"- （旧记忆折叠）{folded[0]['text']} 等 {len(folded)} 条早期印象，渐渐模糊了。",
                    kind="fold",
                )
            rows = keep
        tmp = self.mem_path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            for m in rows:
                fh.write(json.dumps(m, ensure_ascii=False) + "\n")
        tmp.replace(self.mem_path)

    def _promote(self, rows: list[dict[str, Any]]) -> None:
        """命中 >= N 且跨 >= D 天的条目，写进 SOUL（每天最多 max_changes_day 次）。"""
        today = _now().date()
        if self._changes_today() >= self.max_changes_day:
            return
        for m in rows:
            if m.get("promoted") or m.get("forgotten"):
                continue
            first = _dt.datetime.fromtimestamp(float(m.get("first_seen", 0))).date()
            if int(m.get("hits", 1)) < self.promote_hits:
                continue
            if (today - first).days < self.promote_days:
                continue
            if self._append_soul(
                f"- 后来慢慢形成的一点性子：{m['text']}（见过 {m['hits']} 次）",
                kind="promote",
            ):
                m["promoted"] = True
                self._write_memories(rows)

    def _changes_today(self) -> int:
        log = self.dir / "changes.jsonl"
        if not log.exists():
            return 0
        today = f"{_now():%Y-%m-%d}"
        n = 0
        for line in log.read_text(encoding="utf-8").splitlines():
            try:
                if json.loads(line).get("date") == today:
                    n += 1
            except json.JSONDecodeError:
                continue
        return n

    def _append_soul(self, line: str, kind: str) -> bool:
        soul = self.soul_path.read_text(encoding="utf-8").rstrip()
        candidate = soul + "\n" + line + "\n"
        if len(candidate) > self.max_soul_chars:
            return False
        stamp = f"{_now():%Y%m%d-%H%M%S}"
        (self.dir / "history" / f"{stamp}.md").write_text(soul + "\n", encoding="utf-8")
        self.soul_path.write_text(candidate, encoding="utf-8")
        with (self.dir / "changes.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(
                json.dumps({"date": f"{_now():%Y-%m-%d}", "at": stamp, "kind": kind}, ensure_ascii=False)
                + "\n"
            )
        return True
