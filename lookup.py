# -*- coding: utf-8 -*-
"""只读网页搜索：给群聊机器人补「查资料」能力，不接触本地任何东西。

只用 Bing 的 HTML 结果页，不需要 API Key；解析失败返回空列表，调用方降级为
「这次没查到」而不是报错。
"""
from __future__ import annotations

import html
import re
import urllib.parse
import urllib.request

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"

TRIGGERS = ("查一下", "查查", "帮我查", "搜一下", "搜搜", "搜索", "查下", "搜下")
LEADING = ("查一下", "查查", "帮我查一下", "帮我查", "搜一下", "搜搜", "搜索", "查下", "搜下", "查", "搜")


def extract_query(text: str) -> str | None:
    """判断这句话是不是「让我查一下」，是就返回查询词。"""
    t = text.strip()
    if not t or len(t) > 120:
        return None
    hit = next((k for k in TRIGGERS if t.startswith(k)), None)
    if hit is None:
        return None
    q = t[len(hit) :].strip(" ：:，,。.！!？?")
    return q or None


def _strip_tags(s: str) -> str:
    s = re.sub(r"<[^>]+>", "", s)
    return html.unescape(s).strip()


def search(query: str, limit: int = 3, timeout: int = 10) -> list[dict[str, str]]:
    """先查 www.bing.com（稳定），失败再退 cn.bing.com。"""
    for host in ("https://www.bing.com/search", "https://cn.bing.com/search"):
        results = _search_host(host, query, limit, timeout)
        if results:
            return results
    return []


def _search_host(base: str, query: str, limit: int, timeout: int) -> list[dict[str, str]]:
    url = base + "?q=" + urllib.parse.quote(query) + "&mkt=zh-CN&setlang=zh-CN"
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return []
    if "b_algo" not in body:
        return []

    results: list[dict[str, str]] = []
    for block in re.findall(r'<li class="b_algo".*?</li>', body, re.S):
        m = re.search(r'<h2[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
        if not m:
            continue
        link, title = m.group(1), _strip_tags(m.group(2))
        snippet = ""
        sm = re.search(r"<p[^>]*>(.*?)</p>", block, re.S)
        if sm:
            snippet = _strip_tags(sm.group(1))
        if not title:
            continue
        results.append({"title": title[:120], "snippet": snippet[:300], "url": link[:300]})
        if len(results) >= limit:
            break
    return results


def render(results: list[dict[str, str]]) -> str:
    if not results:
        return ""
    lines = ["【联网搜索结果（只读参考，可能过时，别当事实照抄）】"]
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. {r['title']}")
        if r["snippet"]:
            lines.append(f"   {r['snippet']}")
    return "\n".join(lines)
