# -*- coding: utf-8 -*-
"""WorkBuddy（国内版）上游接口的最小客户端。

凭据来自 dsh-connect-workbuddy 刷新后写在 ~/.dsh/workbuddy-vault/cn/*.json 里的
accessToken（明文，仅本机）。这里不复制桌面版凭据，只读插件已经刷新好的那份。

已验证（2026-10-27）：
  POST https://copilot.tencent.com/v2/chat/completions  (stream=true, SSE)
  POST https://www.codebuddy.cn/v2/billing/meter/checkin-activity-status
  POST https://www.codebuddy.cn/v2/billing/meter/daily-checkin
"""
from __future__ import annotations

import glob
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

CHAT_BASE = "https://copilot.tencent.com"
BILLING_BASE = "https://www.codebuddy.cn"
UA = "CLI/2.63.2 CodeBuddy/2.63.2"


class WorkBuddyError(RuntimeError):
    pass


def vault_dir() -> Path:
    return Path(os.environ.get("USERPROFILE", "")) / ".dsh" / "workbuddy-vault" / "cn"


def load_credential(path: str | None = None) -> dict:
    """读取最新的凭据文件；返回 token / uid / domain / expires_at / path。"""
    if path:
        files = [path]
    else:
        files = sorted(glob.glob(str(vault_dir() / "*.json")), key=os.path.getmtime, reverse=True)
    if not files:
        raise WorkBuddyError(
            "找不到 WorkBuddy 凭据（~/.dsh/workbuddy-vault/cn/*.json）。"
            "先跑一次：dsh plugin --profile headless exec dsh-connect-workbuddy status"
        )
    data = json.loads(Path(files[0]).read_text(encoding="utf-8"))
    auth = data.get("auth") or {}
    account = data.get("account") or {}
    token = auth.get("accessToken")
    if not token:
        raise WorkBuddyError("凭据里没有 accessToken，重新跑一次 status 刷新")
    expires = int(auth.get("expiresAt") or 0)
    if expires and expires < time.time() + 60:
        raise WorkBuddyError(
            "WorkBuddy accessToken 已过期。跑一次 "
            "`dsh plugin --profile headless exec dsh-connect-workbuddy status` 让插件刷新后再试"
        )
    return {
        "token": token,
        "uid": account.get("uid", ""),
        "uin": account.get("uin", ""),
        "domain": auth.get("domain", ""),
        "expires_at": expires,
        "path": files[0],
        "nickname": account.get("nickname", ""),
    }


def _headers(cred: dict, sse: bool = False) -> dict:
    headers = {
        "Authorization": f"Bearer {cred['token']}",
        "Accept": "text/event-stream" if sse else "application/json",
        "Content-Type": "application/json",
        "User-Agent": UA,
        "Origin": BILLING_BASE,
        "Referer": BILLING_BASE + "/",
        "X-Requested-With": "XMLHttpRequest",
        "X-Product": "SaaS",
    }
    if cred.get("uid"):
        headers["X-User-Id"] = cred["uid"]
    if cred.get("domain"):
        headers["X-Domain"] = cred["domain"]
    return headers


def _post(url: str, headers: dict, payload: dict, timeout: int) -> tuple[int, str]:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def chat(prompt: str, model: str = "deepseek-v4.1-flash", timeout: int = 120, cred: dict | None = None) -> str:
    """一次性问答：内部用流式，拼成完整文本返回。"""
    cred = cred or load_credential()
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": True,
        "max_tokens": 1200,
    }
    req = urllib.request.Request(
        f"{CHAT_BASE}/v2/chat/completions",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers=_headers(cred, sse=True),
        method="POST",
    )
    parts: list[str] = []
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                raise WorkBuddyError(f"HTTP {resp.status}")
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line or not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    obj = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                code = obj.get("code")
                if code not in (None, 0):
                    msg = obj.get("msg") or ""
                    raise WorkBuddyError(f"上游错误 {code}：{msg}")
                for choice in obj.get("choices") or []:
                    delta = choice.get("delta") or {}
                    if delta.get("content"):
                        parts.append(delta["content"])
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:400]
        raise WorkBuddyError(f"HTTP {exc.code}：{detail}") from None
    except urllib.error.URLError as exc:
        raise WorkBuddyError(f"连不上 WorkBuddy 上游：{exc.reason}") from None

    text = "".join(parts).strip()
    if not text:
        raise WorkBuddyError("WorkBuddy 返回空回复")
    return text


def checkin_status(cred: dict | None = None) -> dict:
    cred = cred or load_credential()
    status, text = _post(
        f"{BILLING_BASE}/v2/billing/meter/checkin-activity-status",
        _headers(cred),
        {},
        timeout=30,
    )
    if status != 200:
        raise WorkBuddyError(f"查询签到状态失败 HTTP {status}：{text[:300]}")
    obj = json.loads(text)
    if obj.get("code") not in (None, 0):
        raise WorkBuddyError(f"查询签到状态失败：{obj.get('msg')}")
    return obj.get("data") or {}


def daily_checkin(cred: dict | None = None) -> dict:
    cred = cred or load_credential()
    status, text = _post(
        f"{BILLING_BASE}/v2/billing/meter/daily-checkin",
        _headers(cred),
        {},
        timeout=30,
    )
    if status != 200:
        raise WorkBuddyError(f"签到失败 HTTP {status}：{text[:300]}")
    obj = json.loads(text)
    if obj.get("code") not in (None, 0):
        raise WorkBuddyError(f"签到失败：{obj.get('msg')}")
    return obj.get("data") or {}


def ensure_checked_in() -> dict:
    """幂等签到：已签就跳过，否则领一次。返回 {action, streak, credit, ...}。"""
    status = checkin_status()
    if status.get("today_checked_in"):
        return {"action": "skip", **status}
    data = daily_checkin()
    after = checkin_status()
    return {"action": "checked_in", "credit": data.get("credit"), **after}
