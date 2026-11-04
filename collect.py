# -*- coding: utf-8 -*-
"""消息段 → 纯文本。

框架 `MessageEvent.plain_text` 只处理 text/image/at，`face` / `record` / `file` /
`json` 段会被**静默丢掉** —— 通知群恰恰最爱发卡片和文件，所以这里自己遍历。

同时提供两种入口：

    event_row(event)    实时事件 → 入库行
    history_row(raw)    历史接口返回的 dict → 入库行
"""
from __future__ import annotations

import json
from typing import Any, Optional

#: 带这些段的消息算「富消息」，渲染时会尽量保留信息量
_TEXT_SEGMENTS = {"text", "at"}

SEGMENT_LABELS = {
    "face": "[表情]",
    "mface": "[表情]",
    "marketface": "[表情]",
    "image": "[图片]",
    "record": "[语音]",
    "video": "[视频]",
    "rps": "[猜拳]",
    "dice": "[骰子]",
    "poke": "[戳一戳]",
    "forward": "[合并转发]",
    "node": "[合并转发]",
    "location": "[位置]",
    "music": "[音乐]",
    "contact": "[名片]",
    "lightapp": "[小程序]",
    "xml": "[卡片]",
}

#: JSON 卡片里值得提取的键（按优先级）
_CARD_KEYS = ("prompt", "title", "desc", "summary", "content", "text", "subtitle")


def _clip(text: str, limit: int = 300) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit] + "…"


def json_card_text(raw: Any) -> str:
    """把 `json` 段的卡片内容拍成一行字。

    群里转发的公告/接龙/作业通知大多是这种卡片，`data.data` 是一串 JSON。
    这里只做「捞出人话」：遍历找 `prompt` / `title` / `desc` 之类的字段，
    不解析具体业务结构。
    """
    if isinstance(raw, str):
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            return _clip(raw) or "[卡片]"
    else:
        payload = raw
    found: list[str] = []

    def walk(node: Any, depth: int = 0) -> None:
        if depth > 6 or len(found) >= 4:
            return
        if isinstance(node, dict):
            for key, value in node.items():
                if key in _CARD_KEYS and isinstance(value, str) and value.strip():
                    piece = _clip(value, 120)
                    if piece and piece not in found:
                        found.append(piece)
                walk(value, depth + 1)
        elif isinstance(node, list):
            for item in node[:20]:
                walk(item, depth + 1)

    walk(payload)
    return " ".join(found) if found else "[卡片]"


def segments_to_text(message: Any, *, self_id: int = 0) -> tuple[str, str]:
    """消息段数组 → `(文本, kind)`；`kind` 为 `text` 或 `rich`。

    `self_id` 用来把「@机器人」渲染成 `@我`，避免总结时把机器人当责任人。
    """
    if isinstance(message, str):
        # 少数后端仍用 CQ 码字符串：原样返回，至少不丢内容
        return message.strip(), "text"
    if not isinstance(message, list):
        return "", "text"

    text, kind, _media = render_segments(message, self_id=self_id)
    return text, kind


#: 一条消息里最多记几个附件（防卡片新闻那种一次几十张图）
MAX_MEDIA_PER_MESSAGE = 6


def media_of_segments(message: Any) -> list[dict]:
    """消息里的图片 / 文件描述（**不下载**，只留坐标）。

    采集阶段只存这些元信息，真正下载与解析留到总结时 —— 采集要轻，
    而且下载会用到 API 节流额度，不该在收消息的路径上做。
    """
    if not isinstance(message, list):
        return []
    items: list[dict] = []
    for seg in message:
        if not isinstance(seg, dict) or len(items) >= MAX_MEDIA_PER_MESSAGE:
            break
        seg_type = str(seg.get("type") or "")
        data = seg.get("data") or {}
        if not isinstance(data, dict):
            continue
        if seg_type == "image":
            items.append({
                "kind": "image",
                "file": str(data.get("file") or ""),
                "file_id": str(data.get("file_id") or ""),
                "url": str(data.get("url") or ""),
                "name": "",
                "size": _as_int(data.get("file_size") or data.get("size")),
            })
        elif seg_type == "file":
            items.append({
                "kind": "file",
                "file": str(data.get("file") or ""),
                "file_id": str(data.get("file_id") or ""),
                "url": str(data.get("url") or ""),
                "name": str(data.get("name") or data.get("file") or "").strip(),
                "size": _as_int(data.get("size") or data.get("file_size")),
            })
    return [m for m in items if m.get("file") or m.get("file_id") or m.get("url")]


def render_segments(message: Any, *, self_id: int = 0) -> tuple[str, str, list[dict]]:
    """消息段数组 → `(文本, kind, 附件描述列表)`。"""
    if isinstance(message, str):
        return message.strip(), "text", []
    if not isinstance(message, list):
        return "", "text", []

    parts: list[str] = []
    rich = False
    for seg in message:
        if not isinstance(seg, dict):
            continue
        seg_type = str(seg.get("type") or "")
        data = seg.get("data") or {}
        if not isinstance(data, dict):
            data = {}
        if seg_type == "text":
            parts.append(str(data.get("text") or ""))
            continue
        if seg_type == "at":
            qq = str(data.get("qq") or "")
            name = str(data.get("name") or "").strip()
            if qq == "all":
                parts.append("@全体成员")
            elif self_id and qq == str(self_id):
                parts.append("@我")
            else:
                parts.append(f"@{name or qq}")
            continue
        if seg_type == "reply":
            continue                      # 引用不携带新信息
        rich = True
        if seg_type == "file":
            name = str(data.get("name") or data.get("file") or "").strip()
            parts.append(f"[文件:{name}]" if name else "[文件]")
            continue
        if seg_type == "json":
            parts.append("[卡片]" + json_card_text(data.get("data") or data))
            continue
        if seg_type == "markdown":
            parts.append("[卡片]" + _clip(str(data.get("content") or ""), 200))
            continue
        if seg_type == "share":
            title = str(data.get("title") or data.get("url") or "").strip()
            parts.append(f"[链接:{title}]" if title else "[链接]")
            continue
        parts.append(SEGMENT_LABELS.get(seg_type, f"[{seg_type or '未知'}]"))

    text = "".join(parts).strip()
    return text, ("rich" if rich else "text"), media_of_segments(message)


def _as_int(value, default: int = 0) -> int:
    try:
        num = int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return default
    return num if num >= 0 else default


def sender_name(sender: Any) -> str:
    """显示名：群名片 > 昵称 > QQ 号。"""
    if sender is None:
        return ""
    if isinstance(sender, dict):
        card = str(sender.get("card") or "").strip()
        nickname = str(sender.get("nickname") or "").strip()
        user_id = sender.get("user_id")
    else:
        card = str(getattr(sender, "card", "") or "").strip()
        nickname = str(getattr(sender, "nickname", "") or "").strip()
        user_id = getattr(sender, "user_id", None)
    return (card or nickname or (str(user_id) if user_id else ""))[:64]


def event_row(event, *, self_id: int = 0) -> Optional[dict]:
    """`MessageEvent` → 入库行；不是群消息或缺关键字段时返回 None。"""
    if getattr(event, "message_type", "") != "group":
        return None
    try:
        group_id = int(getattr(event, "group_id", 0) or 0)
        message_id = int(getattr(event, "message_id", 0) or 0)
    except (TypeError, ValueError):
        return None
    if group_id <= 0 or message_id <= 0:
        return None
    text, kind, media = render_segments(getattr(event, "message", None), self_id=self_id)
    if not text and not media:
        return None
    return {
        "message_id": message_id,
        "seq": int(getattr(event, "message_seq", 0) or 0),
        "user_id": int(getattr(event, "user_id", 0) or 0),
        "sender": sender_name(getattr(event, "sender", None)),
        "text": text or "[附件]",
        "kind": kind,
        "media": media,
        "ts": int(getattr(event, "time", 0) or 0),
    }


def history_row(raw: dict) -> Optional[dict]:
    """`get_group_msg_history` 返回的单条 dict → 入库行。"""
    if not isinstance(raw, dict):
        return None
    try:
        message_id = int(raw.get("message_id") or 0)
    except (TypeError, ValueError):
        return None
    if message_id <= 0:
        return None
    text, kind, media = render_segments(raw.get("message"))
    if not text and not media:
        return None
    sender = raw.get("sender") if isinstance(raw.get("sender"), dict) else {}
    try:
        user_id = int(raw.get("user_id") or sender.get("user_id") or 0)
    except (TypeError, ValueError):
        user_id = 0
    try:
        seq = int(raw.get("message_seq") or 0)
    except (TypeError, ValueError):
        seq = 0
    return {
        "message_id": message_id,
        "seq": seq,
        "user_id": user_id,
        "sender": sender_name(sender) or (str(user_id) if user_id else ""),
        "text": text or "[附件]",
        "kind": kind,
        "media": media,
        "ts": int(raw.get("time") or 0),
    }


__all__ = [
    "SEGMENT_LABELS", "MAX_MEDIA_PER_MESSAGE", "json_card_text",
    "segments_to_text", "render_segments", "media_of_segments",
    "sender_name", "event_row", "history_row",
]
