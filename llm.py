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
    """调一次对话补全，返回正文。失败时抛框架的分类错误（用户看到的是人话 + 编号）。

    **带图降级**：部分 OpenAI 兼容端点不收 `image_url`，会回 400。遇到这种情况
    自动去掉图片重试一次 —— 宁可少看几张图，也别让整次总结失败。
    """
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
        content = await _post(url, payload, headers, timeout)
    except _BadRequest as e:
        if not has_images(messages):
            raise PluginExternalError(
                f"模型接口拒绝请求（HTTP {e.status}）：{e.body[:300]}",
                user_message=f"模型接口报错了（HTTP {e.status}），联系超管看日志") from e
        plugin.logger.warning("[qqnotice_sum] 模型不收图片（HTTP %s），去掉图片重试", e.status)
        payload = dict(payload)
        payload["messages"] = strip_images(messages)
        content = await _post(url, payload, headers, timeout)
    return content


class _BadRequest(Exception):
    def __init__(self, status: int, body: str):
        super().__init__(f"HTTP {status}: {body[:200]}")
        self.status = status
        self.body = body


async def _post(url: str, payload: dict, headers: dict, timeout) -> str:
    """真正发请求；400 单独抛 `_BadRequest`，好让上层决定要不要去掉图片重试。"""
    import json as _json

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
                if resp.status == 400:
                    raise _BadRequest(400, body)
                if resp.status >= 400:
                    raise PluginExternalError(
                        f"模型接口返回 HTTP {resp.status}：{body[:300]}",
                        user_message=f"模型接口报错了（HTTP {resp.status}），联系超管看日志")
                data = _json.loads(body)
    except asyncio.TimeoutError as e:
        raise PluginTimeoutError(
            f"模型接口超时",
            user_message="模型接口超时了，稍后再试或把 llm.timeout 调大") from e
    except aiohttp.ClientError as e:
        raise PluginExternalError(
            f"连接模型接口失败：{e}",
            user_message="连不上模型接口，检查 base_url 与网络") from e
    except _json.JSONDecodeError as e:
        raise PluginExternalError(
            f"模型接口返回的不是 JSON：{e}",
            user_message="模型接口返回格式不对，联系超管看日志") from e

    try:
        return str(data["choices"][0]["message"]["content"] or "")
    except (KeyError, IndexError, TypeError) as e:
        raise PluginExternalError(
            f"模型接口返回结构不认识：{str(data)[:200]}",
            user_message="模型接口返回结构不对，联系超管看日志") from e


def has_images(messages: list[dict]) -> bool:
    for message in messages:
        content = message.get("content")
        if isinstance(content, list) and any(
                isinstance(part, dict) and part.get("type") == "image_url" for part in content):
            return True
    return False


def strip_images(messages: list[dict]) -> list[dict]:
    """把 multimodal content 降级成纯文本（图片位置留一句说明）。"""
    out = []
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            out.append(message)
            continue
        texts = []
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text":
                texts.append(str(part.get("text") or ""))
            elif part.get("type") == "image_url":
                texts.append("[图片：本次未提供给模型]")
        out.append({**message, "content": "\n".join(texts)})
    return out


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


def parse_report(text: str) -> Optional[dict]:
    """从模型回复里抠出 `{"summary","points","todos"}`。

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
    if items:
        return {"summary": "", "points": [], "todos": items}
    return None


def parse_todos(text: str) -> Optional[list[dict]]:
    """只要待办部分（老接口，测试与外部脚本用）。"""
    report = parse_report(text)
    return None if report is None else report["todos"]


def _normalize_report(data: dict) -> dict:
    summary = str(data.get("summary") or data.get("overview") or data.get("概要") or "").strip()
    raw_points = data.get("points") or data.get("key_points") or data.get("要点") or []
    points: list[str] = []
    if isinstance(raw_points, list):
        for item in raw_points:
            if isinstance(item, dict):
                item = item.get("text") or item.get("title") or ""
            text = str(item or "").strip()
            if text:
                points.append(text[:120])
            if len(points) >= 8:
                break
    todos: list[dict] = []
    for key in ("todos", "todo", "items", "tasks", "list"):
        value = data.get(key)
        if isinstance(value, list):
            todos = [x for x in (_normalize_item(v) for v in value) if x]
            break
    return {"summary": summary[:800], "points": points, "todos": todos}


def _from_payload(data) -> Optional[dict]:
    """`{"summary","points","todos"}` / 裸数组 / 单对象 三种形态都认。

    `{"todos":[]}` 是**合法**结果（这段时间确实没事），不能因为三个字段都空就
    判定成「没解析出来」—— 那会让「今天没有待办」变成「解析失败」。
    """
    if isinstance(data, dict):
        known = ("summary", "overview", "概要", "points", "key_points", "要点",
                 "todos", "todo", "items", "tasks", "list")
        if any(key in data for key in known):
            return _normalize_report(data)
        single = _normalize_item(data)
        return {"summary": "", "points": [], "todos": [single]} if single else None
    if isinstance(data, list):
        return {"summary": "", "points": [],
                "todos": [x for x in (_normalize_item(v) for v in data) if x]}
    return None


__all__ = ["chat", "has_images", "strip_images", "parse_report", "parse_todos"]
