# -*- coding: utf-8 -*-
"""wechat-dsh-bot —— 把微信群里的 @ 消息转给 DeepSeek Harness，再把回复发回群里。

设计要点（先看这几条，再看代码）：
  1. 只读微信本地数据库（wechatauto-replica），不 hook、不改客户端；
  2. 发消息走 UIA/OCR 驱动真实窗口，库自带拟人节奏（rhythm），默认 calm；
  3. 只回白名单群、只回 @ 机器人的文本消息；其它一律只记日志；
  4. 每次回复都是一个新的 `dsh --profile headless` 进程（无状态），
     上下文由本程序拼进 prompt，避免会话互相污染。

命令：
  python bot.py check            环境自检（微信 / 数据库 / DSH / 模型）
  python bot.py groups           列出本机所有群（含 username，填配置用）
  python bot.py run [--dry-run]  正式运行（--dry-run 只打印不发送）
  python bot.py send-test        往“文件传输助手”发一条测试消息
  python bot.py dsh-test "问题"  单独试一次 DSH headless 调用
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.yaml"
LOG_DIR = ROOT / "logs"
STATE_DIR = ROOT / "state"

# ---------------------------------------------------------------- 基础工具


def _utf8_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class Log:
    """控制台 + 按天文件，两个地方都写。"""

    def __init__(self) -> None:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._path = LOG_DIR / f"bot-{datetime.now():%Y%m%d}.log"

    def __call__(self, msg: str, level: str = "info") -> None:
        line = f"[{now()}] [{level}] {msg}"
        with self._lock:
            # 无控制台（后台/scheduled）时 sys.stdout 可能是 None，不能让日志把进程打死
            try:
                if sys.stdout is not None:
                    print(line, flush=True)
            except Exception:  # noqa: BLE001
                pass
            try:
                with self._path.open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            except OSError:
                pass


log = Log()


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        cfg = default_config()
        CONFIG_PATH.write_text(
            "# 由 bot.py 自动生成，改完重启生效\n"
            + yaml_dump(cfg),
            encoding="utf-8",
        )
        log(f"未找到 config.yaml，已生成默认配置：{CONFIG_PATH}")
    text = CONFIG_PATH.read_text(encoding="utf-8")
    try:
        import yaml  # type: ignore
    except ImportError:
        sys.exit("缺少 pyyaml：pip install pyyaml")
    cfg = yaml.safe_load(text) or {}
    return cfg


def yaml_dump(data: dict[str, Any]) -> str:
    try:
        import yaml  # type: ignore

        return yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
    except ImportError:
        return json.dumps(data, ensure_ascii=False, indent=2)


def default_config() -> dict[str, Any]:
    return {
        "groups": [],
        "bot": {
            "nicknames": [],
            "trigger_prefixes": ["/ai", "小助手"],
            "reply_prefix": "",
            "max_reply_chars": 600,
            "context_messages": 12,
            "ack_enabled": False,
            "ack_text": "收到，我想一下…",
            "ack_after_sec": 6,
        },
        "limits": {
            "per_group_cooldown_sec": 15,
            "per_group_per_hour": 30,
            "global_per_hour": 60,
            "skip_backlog_sec": 180,
            "rhythm_profile": "calm",
        },
        "backend": {
            "mode": "dsh",
            "dsh_cmd": "",
            "dsh_profile": "headless",
            "work_dir": str(ROOT),
            "timeout_sec": 180,
            "api_base": "https://api.deepseek.com",
            "api_model": "deepseek-chat",
            "api_key_env": "DEEPSEEK_API_KEY",
        },
        "wechat": {
            "listen_private": False,
            "send_verify": True,
        },
    }


def nested(cfg: dict[str, Any], path: str, default: Any = None) -> Any:
    cur: Any = cfg
    for key in path.split("."):
        if not isinstance(cur, dict) or key not in cur:
            return default
        cur = cur[key]
    return cur


# ---------------------------------------------------------------- DSH 调用


class DshError(RuntimeError):
    pass


def find_dsh_cmd(cfg: dict[str, Any]) -> str:
    configured = nested(cfg, "backend.dsh_cmd", "") or ""
    if configured and Path(configured).exists():
        return configured
    candidates = [
        Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
        / "DeepSeek Harness/resources/runtime/cli/bin/dsh.cmd",
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
        / "DeepSeek Harness/resources/runtime/cli/bin/dsh.cmd",
        Path(r"D:\deepseekharness\resources\runtime\cli\bin\dsh.cmd"),
        Path(os.environ.get("LOCALAPPDATA", ""))
        / "Programs/DeepSeek Harness/resources/runtime/cli/bin/dsh.cmd",
    ]
    for path in candidates:
        if path.exists():
            return str(path)
    found = shutil_which("dsh")
    if found:
        return found
    raise DshError(
        "找不到 dsh 启动器，请在 config.yaml 的 backend.dsh_cmd 填绝对路径，"
        r"例如 D:\deepseekharness\resources\runtime\cli\bin\dsh.cmd"
    )


def shutil_which(name: str) -> str | None:
    import shutil

    return shutil.which(name)


class DshClient:
    """每次调用起一个 headless 进程，任务文本走 stdin（不受 32k 命令行限制）。"""

    def __init__(self, cfg: dict[str, Any]) -> None:
        self.cmd = find_dsh_cmd(cfg)
        self.profile = nested(cfg, "backend.dsh_profile", "headless")
        self.cwd = nested(cfg, "backend.work_dir", str(ROOT)) or str(ROOT)
        self.timeout = int(nested(cfg, "backend.timeout_sec", 180))
        self._sem = threading.Semaphore(2)
        self._api_key = resolve_api_key(cfg)

    def call(self, prompt: str) -> str:
        with self._sem:
            env = os.environ.copy()
            if self._api_key and not env.get("DEEPSEEK_API_KEY"):
                env["DEEPSEEK_API_KEY"] = self._api_key
            started = time.time()
            try:
                proc = subprocess.run(
                    ["cmd", "/c", self.cmd, "--profile", self.profile, "-"],
                    input=prompt,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=self.timeout,
                    cwd=self.cwd,
                    env=env,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except subprocess.TimeoutExpired:
                raise DshError(f"DSH 超时（>{self.timeout}s）") from None
            except FileNotFoundError as exc:
                raise DshError(f"DSH 无法启动：{exc}") from None
            elapsed = time.time() - started
            if proc.returncode != 0:
                detail = (proc.stderr or proc.stdout or "").strip()
                detail = detail[-600:]
                raise DshError(f"DSH 退出码 {proc.returncode}：{detail}")
            reply = (proc.stdout or "").strip()
            if not reply:
                raise DshError("DSH 返回空回复")
            log(f"  └ DSH 完成 {elapsed:.1f}s，{len(reply)} 字")
            return reply


def resolve_api_key(cfg: dict[str, Any]) -> str | None:
    """API Key 的来源（按优先级）：
    1. 环境变量（config 里的 api_key_env，默认 DEEPSEEK_API_KEY）
    2. ~/.dsh/.credentials.yaml 里的 refs.DEEPSEEK_API_KEY（DSH 桌面端写进去的那份）
    配置文件里不放密钥。
    """
    env_name = nested(cfg, "backend.api_key_env", "DEEPSEEK_API_KEY")
    if os.environ.get(env_name):
        return os.environ[env_name]
    if os.environ.get("DEEPSEEK_API_KEY"):
        return os.environ["DEEPSEEK_API_KEY"]

    cred = Path(os.environ.get("USERPROFILE", "")) / ".dsh" / ".credentials.yaml"
    if cred.exists():
        try:
            import yaml  # type: ignore

            data = yaml.safe_load(cred.read_text(encoding="utf-8")) or {}
            refs = data.get("refs") or {}
            for name in (env_name, "DEEPSEEK_API_KEY"):
                val = refs.get(name)
                if isinstance(val, str) and val.startswith("sk-"):
                    return val
        except Exception:  # noqa: BLE001
            pass
    return None


class ApiClient:
    """DeepSeek 官方 OpenAI 兼容接口，做“快问快答”兜底通道。"""

    def __init__(self, cfg: dict[str, Any]) -> None:
        self.base = (nested(cfg, "backend.api_base", "https://api.deepseek.com") or "").rstrip("/")
        self.model = nested(cfg, "backend.api_model", "deepseek-chat")
        self.timeout = int(nested(cfg, "backend.timeout_sec", 180))
        self.key = resolve_api_key(cfg)
        if not self.key:
            raise DshError("缺少 API Key（设置环境变量 DEEPSEEK_API_KEY）")
        self._sem = threading.Semaphore(4)

    def call(self, prompt: str) -> str:
        import urllib.error
        import urllib.request

        body = json.dumps(
            {
                "model": self.model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.7,
                "max_tokens": 800,
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base}/chat/completions",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.key}",
            },
        )
        with self._sem:
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                raise DshError(f"API HTTP {exc.code}：{exc.read()[:300]!r}") from None
            except Exception as exc:  # noqa: BLE001
                raise DshError(f"API 调用失败：{exc}") from None
        try:
            return data["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError):
            raise DshError(f"API 返回结构异常：{str(data)[:300]}") from None


class WorkBuddyClient:
    """直接调 WorkBuddy（国内版）上游：读 dsh-connect-workbuddy 刷新好的凭据。

    上游只支持流式，这里在 workbuddy_api 里把 SSE 拼成完整文本。
    """

    def __init__(self, cfg: dict[str, Any]) -> None:
        self.model = str(nested(cfg, "backend.workbuddy_model", "deepseek-v4.1-flash"))
        self.timeout = int(nested(cfg, "backend.timeout_sec", 180))
        self.vault = str(nested(cfg, "backend.workbuddy_vault", "") or "") or None
        self._sem = threading.Semaphore(2)
        try:
            import workbuddy_api  # type: ignore
        except ImportError as exc:  # noqa: BLE001
            raise DshError(f"缺少 workbuddy_api.py：{exc}") from None
        self._api = workbuddy_api

    def call(self, prompt: str) -> str:
        started = time.time()
        with self._sem:
            try:
                cred = self._api.load_credential(self.vault)
                reply = self._api.chat(prompt, self.model, self.timeout, cred)
            except Exception as exc:  # noqa: BLE001
                raise DshError(str(exc)) from None
        log(f"  └ WorkBuddy 完成 {time.time() - started:.1f}s，{len(reply)} 字（{self.model}）")
        return reply


# ---------------------------------------------------------------- 群消息处理


@dataclass
class GroupRule:
    username: str
    nickname: str


@dataclass
class RateState:
    last_reply: float = 0.0
    timestamps: list[float] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.timestamps is None:
            self.timestamps = []


class RateLimiter:
    def __init__(self, cfg: dict[str, Any]) -> None:
        self.cooldown = float(nested(cfg, "limits.per_group_cooldown_sec", 15))
        self.per_group = int(nested(cfg, "limits.per_group_per_hour", 30))
        self.global_limit = int(nested(cfg, "limits.global_per_hour", 60))
        self._lock = threading.Lock()
        self._by_group: dict[str, RateState] = {}
        self._global: list[float] = []

    def allow(self, group: str) -> tuple[bool, str]:
        ts = time.time()
        with self._lock:
            state = self._by_group.setdefault(group, RateState())
            cut = ts - 3600
            state.timestamps = [t for t in state.timestamps if t > cut]
            self._global[:] = [t for t in self._global if t > cut]
            if ts - state.last_reply < self.cooldown:
                return False, f"冷却中（{self.cooldown:.0f}s）"
            if len(state.timestamps) >= self.per_group:
                return False, "本群每小时额度已用完"
            if len(self._global) >= self.global_limit:
                return False, "全局每小时额度已用完"
            state.last_reply = ts
            state.timestamps.append(ts)
            self._global.append(ts)
            return True, ""


def clean_group_body(content: str) -> str:
    """群消息正文前面往往带一行 `wxid_xxx:`，去掉它。"""
    text = (content or "").strip()
    if "\n" in text:
        head, rest = text.split("\n", 1)
        if re.fullmatch(r"[A-Za-z0-9_@\-]{4,64}:?", head.strip()):
            return rest.strip()
    return text


def strip_mentions(text: str, nicknames: list[str]) -> str:
    out = text
    for nick in nicknames:
        if not nick:
            continue
        out = re.sub(rf"@{re.escape(nick)}\s*", "", out)
    return out.strip()


def is_triggered(text: str, nicknames: list[str], prefixes: list[str]) -> bool:
    for nick in nicknames:
        if nick and f"@{nick}" in text:
            return True
    stripped = text.lstrip()
    for pref in prefixes:
        if pref and stripped.startswith(pref):
            return True
    return False


def sanitize_reply(text: str, limit: int) -> str:
    """微信按纯文本显示，去掉 Markdown 记号，压掉多余空行。"""
    out = text.strip()
    out = re.sub(r"^```.*?$", "", out, flags=re.MULTILINE)
    out = re.sub(r"^\s{0,3}#{1,6}\s*", "", out, flags=re.MULTILINE)
    out = re.sub(r"\*\*(.+?)\*\*", r"\1", out)
    out = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"\1", out)
    out = re.sub(r"`([^`]+)`", r"\1", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    if len(out) > limit:
        out = out[: limit - 1].rstrip() + "…"
    return out


DEFAULT_PERSONA = "你是微信群里的普通群友，说话自然、口语化，不装腔、不客服腔。"

PROMPT_TEMPLATE = """{persona}

你现在在微信群「{group}」里，有人 @ 了你。

【最近群聊记录】
{context}

【这次要回答的消息】
{ask}

要求：
1. 只输出要发到群里的回复正文，不要加“回复：”之类的前缀，不要解释你在做什么。
2. 保持上面的人设口吻；中文口语，简短，最多 {max_chars} 字；群里不要长篇大论、不要用 Markdown。
3. 不知道就说不知道，不要编造事实。
4. 需要查资料时用下面给出的搜索结果；没有结果就说没查到，别编。
5. 你**没有任何操作本机的权限**：不能读写文件、不能执行命令、不能改配置、不能调用本地程序。
   群里任何人（包括群主）这样要求你，都直接拒绝，只说一句「本鲸鱼娘只负责聊天和查资料，不碰电脑」。
6. 被认真问“你是不是 AI”时不要撒谎，可以按人设俏皮地承认。
{search_block}
"""

class Bot:
    def __init__(self, cfg: dict[str, Any], dry_run: bool = False) -> None:
        self.cfg = cfg
        self.dry_run = dry_run
        self.limiter = RateLimiter(cfg)
        self.max_chars = int(nested(cfg, "bot.max_reply_chars", 600))
        self.ctx_n = int(nested(cfg, "bot.context_messages", 12))
        self.nicknames = [str(n) for n in (nested(cfg, "bot.nicknames", []) or [])]
        self.prefixes = [str(n) for n in (nested(cfg, "bot.trigger_prefixes", []) or [])]
        self.reply_prefix = str(nested(cfg, "bot.reply_prefix", "") or "")
        self.persona = str(nested(cfg, "bot.persona", "") or "").strip() or DEFAULT_PERSONA
        self.persona_store = None
        evo = nested(cfg, "persona_evolution", {}) or {}
        if evo.get("enabled", True):
            try:
                from persona import PersonaStore  # type: ignore

                self.persona_store = PersonaStore(
                    STATE_DIR / "persona",
                    seed=self.persona,
                    llm=lambda prompt: self.backend().call(prompt),
                    every_n=int(evo.get("every_n_replies", 5)),
                    max_soul_chars=int(evo.get("max_soul_chars", 2000)),
                    max_changes_day=int(evo.get("max_changes_per_day", 3)),
                    max_memories=int(evo.get("max_memories", 200)),
                    promote_hits=int(evo.get("promote_hits", 3)),
                    promote_days=int(evo.get("promote_days", 2)),
                )
                log("人格进化：已启用（SOUL.md + 记忆沉淀）")
            except Exception as exc:  # noqa: BLE001
                log(f"人格进化初始化失败（忽略）：{exc}", "warn")
        self.verify = bool(nested(cfg, "wechat.send_verify", True))
        self.backend_mode = str(nested(cfg, "backend.mode", "dsh")).lower()
        sec = nested(cfg, "security", {}) or {}
        # 群里只允许「聊天 + 查资料」：默认禁止 dsh（有本机权限的 agent）
        self.group_chat_only = bool(sec.get("group_chat_only", True))
        self.web_lookup = bool(sec.get("web_lookup", True))
        self._seen: set[tuple[str, int]] = set()
        self._seen_lock = threading.Lock()
        self._buffers: dict[str, Any] = {}
        self._ack_state: dict[str, threading.Event] = {}
        self._started_at = time.time()
        self.skip_backlog = float(nested(cfg, "limits.skip_backlog_sec", 180))
        self.ack_enabled = bool(nested(cfg, "bot.ack_enabled", False))
        self.ack_text = str(nested(cfg, "bot.ack_text", "收到，我想一下…"))
        self.ack_after = float(nested(cfg, "bot.ack_after_sec", 6))
        self._dsh: DshClient | None = None
        self._api: ApiClient | None = None
        self._wb: WorkBuddyClient | None = None
        self._last_replies: dict[str, list[str]] = {}
        self._self_wxid = ""
        self._self_nick = ""
        self.loopback_mode = False
        self.db = None
        self.listener = None
        self.groups: list[GroupRule] = []

    # -- 初始化 ---------------------------------------------------

    def open_wechat(self) -> None:
        try:
            from wechatauto import WeChatDB, WxParam, rhythm  # type: ignore
        except ImportError as exc:  # noqa: BLE001
            sys.exit(
                "缺少 wechatauto-replica：\n"
                "  C:\\Users\\...\\Python312\\python.exe -m pip install wechatauto-replica\n"
                f"原始错误：{exc}"
            )
        # 微信写库是异步的，默认 8 秒在群多/机器慢时经常读不到（消息其实已发出）。
        try:
            WxParam.DB_VERIFY_TIMEOUT = 20.0
            WxParam.DB_VERIFY_INTERVAL = 2.0
        except Exception:  # noqa: BLE001
            pass
        profile = nested(self.cfg, "limits.rhythm_profile", "calm")
        try:
            rhythm.set_profile(profile)
            log(f"拟人节奏档位：{profile}")
        except Exception as exc:  # noqa: BLE001
            log(f"设置 rhythm 档位失败（忽略）：{exc}", "warn")

        log("正在打开微信数据库（首次需要 6 秒左右提取密钥）…")
        self.db = WeChatDB()
        info = self.db.get_self_info()
        self._self_wxid = info.get("username", "") or ""
        self._self_nick = info.get("nick_name", "") or info.get("remark", "") or ""
        log(f"微信账号：{self._self_nick}（{self._self_wxid}）")
        if not self.nicknames:
            if self._self_nick:
                self.nicknames = [self._self_nick]
                log(f"未配置 bot.nicknames，用账号昵称当 @ 名：{self._self_nick}")
            else:
                log("警告：拿不到账号昵称，@ 触发可能不工作，请手工填 bot.nicknames", "warn")

    def resolve_groups(self) -> list[GroupRule]:
        wanted = [str(g).strip() for g in (nested(self.cfg, "groups", []) or []) if str(g).strip()]
        test_chats = [str(g).strip() for g in (nested(self.cfg, "test_chats", []) or []) if str(g).strip()]
        if not wanted and not test_chats:
            log("config.yaml 的 groups 为空：先用 `python bot.py groups` 看群列表再填。", "error")
            return []
        rules: list[GroupRule] = []
        sessions = {}
        try:
            for item in self.db.get_sessions(limit=500):
                sessions[item["username"]] = item
        except Exception as exc:  # noqa: BLE001
            log(f"读取会话列表失败：{exc}", "warn")

        def resolve(name: str, require_group: bool) -> None:
            if name in sessions and (name.endswith("@chatroom") or not require_group):
                nick = (self.db.get_nickname(name) or name).strip()
                rules.append(GroupRule(name, nick))
                return
            try:
                hits = self.db.search_contact(name) or []
            except Exception as exc:  # noqa: BLE001
                log(f"搜索「{name}」失败：{exc}", "warn")
                hits = []
            if require_group:
                hits = [h for h in hits if str(h.get("username", "")).endswith("@chatroom")]
            exact = [h for h in hits if h.get("nick_name") == name or h.get("remark") == name]
            pick = exact or hits
            if len(pick) == 1:
                nick = (self.db.get_nickname(pick[0]["username"]) or name).strip()
                rules.append(GroupRule(pick[0]["username"], nick))
            elif not pick:
                log(f"找不到「{name}」，已跳过（检查名字是否完全一致）", "error")
            else:
                log(f"「{name}」有 {len(pick)} 个同名结果，请改用 username：" +
                    ", ".join(str(h.get("username")) for h in pick), "error")

        for name in wanted:
            resolve(name, True)
        for name in test_chats:
            log(f"测试会话（不做 @chatroom 过滤）：{name}")
            resolve(name, False)
        for rule in rules:
            log(f"监听：{rule.nickname}（{rule.username}）")
        return rules

    # -- 消息处理 -------------------------------------------------

    def backend(self, mode: str | None = None):
        mode = (mode or self.backend_mode).lower()
        if mode == "dsh" and self.group_chat_only:
            raise DshError(
                "群里禁止使用 dsh 后端（headless agent 有本机权限）。"
                "把 backend.mode 改成 workbuddy 或 api 再启动。"
            )
        if mode == "api":
            if self._api is None:
                self._api = ApiClient(self.cfg)
            return self._api
        if mode == "workbuddy":
            if self._wb is None:
                self._wb = WorkBuddyClient(self.cfg)
            return self._wb
        if self._dsh is None:
            self._dsh = DshClient(self.cfg)
        return self._dsh

    def on_message(self, msg: dict[str, Any], chat_username: str) -> None:
        rule = self._rules_by_username.get(chat_username)
        if rule is None:
            return
        if msg.get("type") != "文本":
            return
        key = (chat_username, int(msg.get("local_id") or 0))
        with self._seen_lock:
            if key in self._seen:
                return
            self._seen.add(key)
            if len(self._seen) > 20000:
                self._seen.clear()

        created = msg.get("create_time")
        if isinstance(created, (int, float)) and created > 1e12:
            created /= 1000.0
        if isinstance(created, (int, float)) and created < self._started_at - self.skip_backlog:
            return  # 启动前的历史消息，不回

        body = clean_group_body(msg.get("content", ""))
        if not body:
            return
        sender = self._sender_name(msg)
        self._record(chat_username, sender, body)
        if sender == "我":
            # 回环自测：自己发出的 #ping 消息当成入站命令，其余自己的消息忽略
            if self.loopback_mode and body.startswith("#ping"):
                body = body[len("#ping") :].strip()
            else:
                return

        if not is_triggered(body, self.nicknames, self.prefixes):
            return

        ask = strip_mentions(body, self.nicknames)
        for pref in self.prefixes:
            if ask.startswith(pref):
                ask = ask[len(pref) :].strip()
                break
        if not ask:
            self._reply(rule, "在，直接说事就行～")
            return

        # 只读联网搜索：机器人自己查，不经过任何本地 agent
        search_block = ""
        if self.web_lookup:
            try:
                import lookup  # type: ignore

                query = lookup.extract_query(ask)
                if query:
                    results = lookup.search(query, limit=3)
                    search_block = lookup.render(results)
                    log(f"[查] {query} → {len(results)} 条结果")
            except Exception as exc:  # noqa: BLE001
                log(f"联网搜索失败（忽略）：{exc}", "warn")

        ok, reason = self.limiter.allow(rule.username)
        if not ok:
            log(f"[跳过] {rule.nickname}：{reason}｜{ask[:40]}")
            return

        log(f"[收到] {rule.nickname}：{ask[:60]}")
        prompt = self.build_prompt(rule, ask, search_block)
        started = time.time()
        ack = self._arm_ack(rule)
        try:
            reply = self.backend().call(prompt)
        except DshError as exc:
            log(f"[失败] {exc}", "error")
            self._disarm_ack(ack)
            self._reply(rule, f"我这边出错了：{str(exc)[:120]}")
            return
        except Exception as exc:  # noqa: BLE001
            log(f"[失败] 未预期错误：{exc}", "error")
            self._disarm_ack(ack)
            return
        finally:
            self._disarm_ack(ack)

        reply = sanitize_reply(reply, self.max_chars)
        if not reply:
            return
        recent = self._last_replies.setdefault(rule.username, [])
        if reply in recent:
            log(f"[跳过] 回复与最近重复（防复读）：{reply[:40]}")
            return
        recent.append(reply)
        del recent[:-3]
        log(f"[回复] {time.time() - started:.1f}s｜{reply[:80]}")
        self._reply(rule, reply)
        if self.persona_store is not None:
            try:
                self.persona_store.observe(ask, reply, rule.nickname)
            except Exception as exc:  # noqa: BLE001
                log(f"人格观察失败（忽略）：{exc}", "warn")

    # -- 上下文 / 思考提示 ----------------------------------------

    def _record(self, chat: str, sender: str, body: str) -> None:
        from collections import deque

        buf = self._buffers.get(chat)
        if buf is None:
            buf = deque(maxlen=max(4, self.ctx_n * 2))
            self._buffers[chat] = buf
        buf.append((sender, body[:200]))

    def _arm_ack(self, rule: GroupRule) -> threading.Event | None:
        if not self.ack_enabled:
            return None
        done = threading.Event()
        self._ack_state[rule.username] = done

        def fire() -> None:
            if done.wait(self.ack_after):
                return  # 已经回完了，不用再提示
            log(f"[ack] {rule.nickname} 还没算完，先发一句提示")
            self._reply(rule, self.ack_text)

        timer = threading.Thread(target=fire, daemon=True)
        timer.start()
        return done

    def _disarm_ack(self, done: threading.Event | None) -> None:
        if done is not None:
            done.set()

    def _sender_name(self, msg: dict[str, Any]) -> str:
        if msg.get("sender_id") in (2, "2"):
            return "我"
        sender = msg.get("sender_username") or ""
        if sender and self._self_wxid and sender == self._self_wxid:
            return "我"
        if sender:
            try:
                return self.db.get_nickname(sender) or sender
            except Exception:  # noqa: BLE001
                return str(sender)
        return "群友"

    def build_prompt(self, group: GroupRule, ask: str, search_block: str = "") -> str:
        buf = self._buffers.get(group.username)
        context = ""
        if buf:
            context = "\n".join(f"{nick}：{text}" for nick, text in list(buf)[-self.ctx_n :])
        if not context:
            lines: list[str] = []
            try:
                rows = self.db.get_messages(group.username, limit=max(1, self.ctx_n))
                for row in reversed(rows):
                    body = clean_group_body(row.get("content", ""))
                    if not body or body.startswith("[非文本"):
                        continue
                    nick = "我" if row.get("sender_id") in (2, "2") else "群友"
                    lines.append(f"{nick}：{body[:160]}")
                context = "\n".join(lines[-self.ctx_n :])
            except Exception as exc:  # noqa: BLE001
                log(f"取上下文失败（不影响回复）：{exc}", "warn")
        context = context or "（无历史消息）"
        persona_text = (
            self.persona_store.render() if self.persona_store is not None else self.persona
        )
        return PROMPT_TEMPLATE.format(
            persona=persona_text,
            group=group.nickname,
            context=context,
            ask=ask,
            max_chars=self.max_chars,
            search_block=search_block,
        )

    def _reply(self, rule: GroupRule, text: str) -> None:
        text = (self.reply_prefix + text).strip()
        if self.dry_run:
            log(f"[DRY-RUN] 本应发到「{rule.nickname}」：{text}")
            return
        try:
            from wechatauto import WxParam  # type: ignore
            from wechatauto.guia import quick_send  # type: ignore

            try:
                WxParam.DB_VERIFY_TIMEOUT = 20.0
                WxParam.DB_VERIFY_INTERVAL = 2.0
            except Exception:  # noqa: BLE001
                pass
            resp = quick_send(text, rule.nickname, verify=self.verify)
        except Exception as exc:  # noqa: BLE001
            log(f"发送异常：{exc}", "error")
            return
        if isinstance(resp, dict):
            status = str(resp.get("status", ""))
            message = str(resp.get("message", ""))
            if status == "失败" and "数据库未确认" in message:
                log("已操作发送，但回读未确认（微信写库延迟，通常已送达）", "warn")
                return
            if status == "失败":
                log(f"发送失败：{message}", "error")
            return
        if resp is False:
            log("发送返回失败（窗口被遮挡/锁屏？）", "error")

    # -- 主循环 ---------------------------------------------------

    def run(self) -> None:
        from wechatauto.db import Listener  # type: ignore

        if self.backend_mode == "dsh" and self.group_chat_only:
            log(
                "拒绝启动：backend.mode=dsh 是带本机权限的 agent，群里只允许聊天/查资料。"
                "请改成 workbuddy 或 api。",
                "error",
            )
            return
        self.open_wechat()
        self.groups = self.resolve_groups()
        if not self.groups:
            return
        self._rules_by_username = {g.username: g for g in self.groups}
        interval = 1.0
        self.listener = Listener(self.db, interval=interval)
        for group in self.groups:
            self.listener.add_listener(
                group.username,
                lambda msg, lst, _u=group.username: self.on_message(msg, _u),
            )
        self.listener.start()
        log(f"已启动，监听 {len(self.groups)} 个群；Ctrl+C 退出。模式={self.backend_mode} dry_run={self.dry_run}")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            log("收到 Ctrl+C，正在停止…")
        finally:
            try:
                self.listener.stop()
            except Exception:  # noqa: BLE001
                pass
            log("已退出")


# ---------------------------------------------------------------- 自检 / 工具


def cmd_check(cfg: dict[str, Any]) -> int:
    print(f"== 配置文件：{CONFIG_PATH}")
    ok = True

    # 1) 依赖
    try:
        import wechatauto  # type: ignore

        print(f"[OK] wechatauto-replica {wechatauto.__version__}")
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] wechatauto-replica 不可用：{exc}")
        ok = False

    mode = str(nested(cfg, "backend.mode", "dsh")).lower()
    print(f"[OK] 后端模式：{mode}")

    # 3) DSH 启动器
    need_dsh = mode == "dsh"
    try:
        dsh = find_dsh_cmd(cfg)
        print(f"[OK] dsh 启动器：{dsh}")
        try:
            out = subprocess.run(
                ["cmd", "/c", dsh, "--version"],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
            )
            print(f"     --version -> {out.stdout.strip() or out.stderr.strip()}")
        except Exception as exc:  # noqa: BLE001
            print(f"     --version 失败：{exc}")
    except DshError as exc:
        print(("[FAIL] " if need_dsh else "[WARN] ") + str(exc))
        if need_dsh:
            ok = False

    # 4) 模型凭据
    key = resolve_api_key(cfg)
    if key:
        print(f"[OK] DEEPSEEK_API_KEY 已就绪（{key[:4]}…{key[-4:]}，长 {len(key)}）")
    else:
        print("[FAIL] 没有 DEEPSEEK_API_KEY：headless 会报 MISSING_CREDENTIAL")
        ok = False

    # 5) 微信数据库
    try:
        from wechatauto import WeChatDB  # type: ignore

        db = WeChatDB()
        info = db.get_self_info()
        print(f"[OK] 微信账号：{info.get('nick_name')}（{info.get('username')}）")
        rooms = [s for s in db.get_sessions(limit=200) if str(s.get("username", "")).endswith("@chatroom")]
        print(f"[OK] 可读群会话 {len(rooms)} 个")
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] 读取微信数据库失败：{exc}")
        print("       微信要已登录、未锁在托盘；首次会扫内存提密钥。")
        ok = False

    # 6) 配置完整性
    groups = nested(cfg, "groups", []) or []
    if groups:
        print(f"[OK] 配置了 {len(groups)} 个群：{', '.join(map(str, groups))}")
    else:
        print("[WARN] groups 还是空的：先跑 `python bot.py groups`")

    print("\n结论：" + ("可以启动。" if ok else "上面有 FAIL，先修完再启动。"))
    return 0 if ok else 1


def cmd_groups(cfg: dict[str, Any]) -> int:
    from wechatauto import WeChatDB  # type: ignore

    db = WeChatDB()
    rows = [s for s in db.get_sessions(limit=500) if str(s.get("username", "")).endswith("@chatroom")]
    if not rows:
        print("没有读到群会话。")
        return 1
    print(f"{'未读':>4}  {'群名':<28} username")
    for s in rows:
        nick = db.get_nickname(s["username"]) or ""
        print(f"{s.get('unread', 0):>4}  {nick:<28} {s['username']}")
    print(f"\n共 {len(rows)} 个群。把要监听的群名（或 username）填进 config.yaml 的 groups。")
    return 0


def cmd_dsh_test(cfg: dict[str, Any], question: str) -> int:
    client = DshClient(cfg)
    print(f"调用：{client.cmd} --profile {client.profile}")
    print(f"问题：{question}")
    started = time.time()
    try:
        print("---- 回复 ----")
        print(client.call(question))
    except DshError as exc:
        print(f"失败：{exc}")
        return 1
    print(f"---- 用时 {time.time() - started:.1f}s ----")
    return 0


def cmd_wb_test(cfg: dict[str, Any], question: str) -> int:
    client = WorkBuddyClient(cfg)
    try:
        cred = client._api.load_credential(client.vault)
        print(f"WorkBuddy：{cred.get('nickname') or cred.get('uid')}")
        print(f"凭据：{cred.get('path')}")
    except Exception as exc:  # noqa: BLE001
        print(f"凭据读取失败：{exc}")
        return 1
    print(f"模型：{client.model}")
    print(f"问题：{question}")
    started = time.time()
    try:
        print("---- 回复 ----")
        print(client.call(question))
    except DshError as exc:
        print(f"失败：{exc}")
        return 1
    print(f"---- 用时 {time.time() - started:.1f}s ----")
    return 0


def cmd_send_test(cfg: dict[str, Any], text: str) -> int:
    from wechatauto import rhythm  # type: ignore
    from wechatauto.guia import quick_send  # type: ignore

    rhythm.set_profile(nested(cfg, "limits.rhythm_profile", "calm"))
    print(f"发送到「文件传输助手」：{text}")
    ok = quick_send(text, "filehelper", verify=True)
    print(f"结果：{ok}")
    return 0 if ok is not False else 1


def cmd_selftest(cfg: dict[str, Any], dry_run: bool) -> int:
    """用「文件传输助手」跑一遍完整链路：解析 → 拼 prompt → DSH → 发送 → 回读。

    入站消息是伪造的（不需要别人真发），出站是真发，所以发送链路是实测。
    """
    bot = Bot(cfg, dry_run=dry_run)
    bot.open_wechat()
    if not bot.nicknames:
        bot.nicknames = [bot._self_nick or "机器人"]
    rule = GroupRule("filehelper", "文件传输助手")
    bot._rules_by_username = {rule.username: rule}

    mention = f"@{bot.nicknames[0]}"
    text = f"{mention} 用一句话介绍你自己，并说明你收到了这条自测消息"
    fake = {
        "type": "文本",
        "content": f"自测用户:\n{text}",
        "local_id": int(time.time() * 1000) % 2_000_000_000,
        "create_time": time.time(),
        "sender_id": 999999,
    }
    print(f"模拟群消息：{text}")
    print("调用 DSH 并发送到「文件传输助手」…")
    started = time.time()
    bot.on_message(fake, rule.username)

    time.sleep(3)
    print(f"\n耗时 {time.time() - started:.1f}s。回读最近消息：")
    try:
        for row in bot.db.get_messages("filehelper", limit=3):
            print(f"  [{row.get('create_time')}] sender={row.get('sender_id')} "
                  f"{str(row.get('content'))[:120]}")
    except Exception as exc:  # noqa: BLE001
        print(f"  回读失败：{exc}")
    print("\n如果上面最后一条是机器人回复，说明读库 / DSH / 发送 三段都通了。")
    return 0


def cmd_loopback(cfg: dict[str, Any]) -> int:
    """回环测试：自己往「文件传输助手」发一条 `#ping`，让真实 Listener 收到后走完整流程。

    这条路径覆盖：读库增量 → Listener 回调 → @ 判定 → 拼 prompt → DSH → 发送 → 库回读。
    """
    from wechatauto.db import Listener  # type: ignore
    from wechatauto.guia import quick_send  # type: ignore

    bot = Bot(cfg)
    bot.open_wechat()
    bot.loopback_mode = True
    if not bot.nicknames:
        bot.nicknames = [bot._self_nick or "机器人"]
    rule = GroupRule("filehelper", "文件传输助手")
    bot._rules_by_username = {rule.username: rule}

    listener = Listener(bot.db, interval=1.0)
    listener.add_listener("filehelper", lambda msg, _l: bot.on_message(msg, rule.username))
    listener.start()
    print("Listener 已启动，2 秒后发回环消息…")
    time.sleep(2)

    before = {row.get("local_id") for row in bot.db.get_messages("filehelper", limit=20)}
    marker = f"#ping @{bot.nicknames[0]} 用一句话说明你收到了回环测试"
    started = time.time()
    resp = quick_send(marker, "文件传输助手", verify=False)
    status = resp.get("status") if isinstance(resp, dict) else resp
    print(f"已发出回环消息（status={status}）：{marker}")

    deadline = time.time() + 180
    reply: str | None = None
    while time.time() < deadline:
        time.sleep(3)
        try:
            rows = bot.db.get_messages("filehelper", limit=10)
        except Exception:  # noqa: BLE001
            continue
        for row in rows:
            if row.get("local_id") in before:
                continue
            content = (row.get("content") or "").strip()
            if not content or content == marker or content.startswith("#ping"):
                continue
            reply = content
            break
        if reply:
            break
    listener.stop()

    if reply:
        print(f"\n[PASS] 回环成功，用时 {time.time() - started:.0f}s")
        print(f"机器人回复：{reply}")
        return 0
    print("\n[FAIL] 180 秒内没有看到回复。检查 logs/bot-*.log 与 DSH 是否可用。")
    return 1


def main() -> int:
    _utf8_console()
    parser = argparse.ArgumentParser(description="微信群 + DeepSeek Harness 桥接机器人")
    parser.add_argument("command", nargs="?", default="run",
                        choices=["run", "check", "groups", "send-test", "dsh-test", "selftest", "loopback", "wb-test"])
    parser.add_argument("--dry-run", action="store_true", help="只打印回复，不真正发送")
    parser.add_argument("--text", default="你好，我是 DSH 桥接测试消息。")
    parser.add_argument("question", nargs="*", help="dsh-test 的问题")
    args = parser.parse_args()

    cfg = load_config()
    if args.command == "check":
        return cmd_check(cfg)
    if args.command == "groups":
        return cmd_groups(cfg)
    if args.command == "send-test":
        return cmd_send_test(cfg, args.text)
    if args.command == "dsh-test":
        return cmd_dsh_test(cfg, " ".join(args.question) or "用一句话介绍你自己")
    if args.command == "wb-test":
        return cmd_wb_test(cfg, " ".join(args.question) or "只回复两个字：可用")
    if args.command == "selftest":
        return cmd_selftest(cfg, args.dry_run)
    if args.command == "loopback":
        return cmd_loopback(cfg)
    return run_foreground(cfg, args.dry_run)


def run_foreground(cfg: dict[str, Any], dry_run: bool) -> int:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = STATE_DIR / "bot.lock"
    lock_fh = lock_path.open("w")
    lock_fh.write("0")
    lock_fh.flush()
    lock_fh.seek(0)
    try:
        import msvcrt

        msvcrt.locking(lock_fh.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        print("已经有一个 bot 实例在跑（state/bot.lock 被占用）。")
        return 1
    try:
        bot = Bot(cfg, dry_run=dry_run)
        bot.run()
    finally:
        try:
            lock_fh.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            lock_path.unlink()
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
