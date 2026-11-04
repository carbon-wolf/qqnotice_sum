# -*- coding: utf-8 -*-
"""OpenAI 兼容的 `chat/completions` 调用，以及待办 JSON 的容错解析。

框架本身零 AI 依赖（`requirements.txt` 只有 websockets / pydantic / aiohttp /
aiofiles），所以这里直接用 aiohttp，配置全部来自 `config.json` 的
`plugins.qqnotice_sum.llm`。

DeepSeek / 通义 / 自建 vLLM / Ollama 的 OpenAI 兼容口都吃这一套：
改 `base_url` 与 `model` 就行。
"""
from __future__ import annotations

import asyncio
import json
import re
from typing import Optional

import aiohttp

from napcat.errors import PluginConfigError, PluginExternalError, PluginRateLimitError
from napcat.errors import PluginTimeoutError

_FENCE_RE = re.compile(r"^\s*```(?:json|JSON)?\s*|\s*```\s*$", re.MULTILINE)
_OBJECT_RE = re.compile(r"\{[^{}]*\}")


def _base_url(cfg: dict) -> str:
    url = str(cfg.get("base_url") or "").rstrip("/")
    if not url:
        raise PluginConfigError(
            "没配模型地址（plugins.qqnotice_sum.llm.base_url）",
            user_message="插件没配模型地址，找超管在 config.json 的 plugins.qqnotice_sum.llm.base_url 里补上")
    if url.endswith("/chat/completions"):
        return url
    return url + "/chat/completions"


async def chat(plugin, messages: list[dict], *, model: str = "") -> str:
    """调一次对话补全，返回正文。失败时抛框架的分类错误（用户看到的是人话 + 编号）。"""
    cfg = plugin.llm_cfg()
    api_key = cfg.get("api_key") or ""
    if not api_key:
        raise PluginConfigError(
            "未配置 llm.api_key",
            user_message="还没配模型密钥：让超管在 config.json 的 "
                         "plugins.qqnotice_sum.llm.api_key 里填上")
    use_model = (model or cfg.get("model") or "").strip()
    if not use_model:
        raise PluginConfigError(
            "未配置 llm.model",
            user_message="没配模型名：让超管在 plugins.qqnotice_sum.llm.model 里填上")

    payload = {
        "model": use_model,
        "messages": messages,
        "temperature": cfg.get("temperature", 0.2),
        "stream": False,
    }
    headers = {"Authorization": f"Bearer {api_key}",
               "Content-Type": "application/json"}
    timeout = aiohttp.ClientTimeout(total=float(cfg.get("timeout") or 120))
    url = _base_url(cfg)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=payload, headers=headers) as resp:
                body = await resp.text()
                if resp.status in (401, 403):
                    raise PluginConfigError(
                        f"模型接口鉴权失败（HTTP {resp.status}）",
                        user_message="模型密钥无效或没权限，检查 llm.api_key")
                if resp.status == 429:
                    raise PluginRateLimitError(
                        f"模型接口限流（HTTP 429）：{body[:200]}",
                        user_message="模型接口被限流了，过一会儿再试")
                if resp.status >= 400:
                    raise PluginExternalError(
                        f"模型接口返回 HTTP {resp.status}：{body[:300]}",
                        user_message=f"模型接口报错了（HTTP {resp.status}），联系超管看日志")
                data = json.loads(body)
    except asyncio.TimeoutError as e:
        raise PluginTimeoutError(
            f"模型接口超时（{cfg.get('timeout')}s）",
            user_message="模型接口超时了，稍后再试或把 llm.timeout 调大") from e
    except aiohttp.ClientError as e:
        raise PluginExternalError(
            f"连接模型接口失败：{e}",
            user_message="连不上模型接口，检查 base_url 与网络") from e
    except json.JSONDecodeError as e:
        raise PluginExternalError(
            f"模型接口返回的不是 JSON：{e}",
            user_message="模型接口返回格式不对，联系超管看日志") from e

    try:
        return str(data["choices"][0]["message"]["content"] or "")
    except (KeyError, IndexError, TypeError) as e:
        raise PluginExternalError(
            f"模型接口返回结构不认识：{str(data)[:200]}",
            user_message="模型接口返回结构不对，联系超管看日志") from e


# ------------------------------------------------------------------ 解析

def _normalize_item(raw: dict) -> Optional[dict]:
    if not isinstance(raw, dict):
        return None
    title = str(raw.get("title") or raw.get("task") or raw.get("事项") or "").strip()
    if not title:
        return None
    priority = str(raw.get("priority") or "normal").strip().lower()
    if priority not in ("high", "normal", "low"):
        # 模型偶尔写「高」「紧急」，顺手归一
        priority = "high" if priority in ("高", "紧急", "urgent", "critical") else "normal"
    return {
        "title": title[:120],
        "owner": str(raw.get("owner") or raw.get("负责人") or "").strip()[:64],
        "due": str(raw.get("due") or raw.get("deadline") or raw.get("截止") or "").strip()[:32],
        "priority": priority,
        "evidence": str(raw.get("evidence") or raw.get("source") or "").strip()[:500],
    }


def parse_todos(text: str) -> Optional[list[dict]]:
    """从模型回复里抠出待办数组。

    三级容错：整段是 JSON → 剥掉代码围栏/前后废话再解析 → 逐个 `{...}` 兜底。
    都失败返回 None（调用方据此提示「本次未结构化」，而不是假装成功）。
    """
    raw = str(text or "").strip()
    if not raw:
        return None

    candidates: list[str] = []
    candidates.append(_FENCE_RE.sub("", raw).strip())
    start, end = raw.find("{"), raw.rfind("}")
    if start >= 0 and end > start:
        candidates.append(raw[start:end + 1])

    for chunk in candidates:
        try:
            data = json.loads(chunk)
        except (TypeError, ValueError):
            continue
        got = _from_payload(data)
        if got is not None:
            return got

    # 兜底：把每个最外层 {...} 单独解析，挑带 title 的
    items: list[dict] = []
    for piece in _OBJECT_RE.findall(raw):
        try:
            data = json.loads(piece)
        except (TypeError, ValueError):
            continue
        item = _normalize_item(data)
        if item is not None:
            items.append(item)
    return items or None


def _from_payload(data) -> Optional[list[dict]]:
    """`{"todos": [...]}` / 裸数组 / 单对象 三种形态都认。"""
    if isinstance(data, dict):
        for key in ("todos", "todo", "items", "tasks", "list"):
            value = data.get(key)
            if isinstance(value, list):
                items = [x for x in (_normalize_item(v) for v in value) if x]
                return items
        single = _normalize_item(data)
        return [single] if single else None
    if isinstance(data, list):
        items = [x for x in (_normalize_item(v) for v in data) if x]
        return items
    return None


__all__ = ["chat", "parse_todos"]
