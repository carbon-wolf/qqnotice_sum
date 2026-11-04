# -*- coding: utf-8 -*-
"""把一堆群消息压成「模型看得懂、又不太长」的对话记录。

三步：清洗（丢噪声）→ 合并（同一人连发）→ 裁剪（字数预算）。
统计口径单独返回，渲染时会用（「128 条消息，筛掉 61 条」比只给结论可信）。
"""
from __future__ import annotations

import re
import time
from typing import Iterable

#: 全是这些占位符的消息没有信息量（纯表情、纯图片、纯「[语音]」）
_PLACEHOLDER_ONLY = re.compile(
    r"^(?:\[(?:表情|图片|语音|视频|骰子|猜拳|戳一戳|合并转发|位置|音乐|名片|小程序|链接)\])+$"
)

#: 归一化用：去掉空白与标点，只留字
_NOISE_STRIP = re.compile(r"[\s\u3000,，。.;；:：!！?？、'\"“”‘’()（）\[\]【】<>《》\-—_/\\|·~`*#]+")


def _compact(text: str) -> str:
    return _NOISE_STRIP.sub("", str(text or "")).lower()


def is_noise(text: str, cfg: dict) -> bool:
    """这条消息值不值得进模型上下文。"""
    raw = str(text or "").strip()
    if not raw:
        return True
    if _PLACEHOLDER_ONLY.match(raw):
        return True
    compact = _compact(raw)
    if len(compact) < int(cfg.get("min_len") or 0):
        return True
    noise = {_compact(x) for x in (cfg.get("noise") or [])}
    return compact in noise


def clean_rows(rows: Iterable[dict], cfg: dict) -> list[dict]:
    """过滤噪声，顺便把每条消息裁到 `max_msg_chars`。"""
    limit = int(cfg.get("max_msg_chars") or 500)
    out = []
    for row in rows:
        text = str(row.get("text") or "").strip()
        if is_noise(text, cfg):
            continue
        if len(text) > limit:
            text = text[:limit] + "…"
        item = dict(row)
        item["text"] = text
        out.append(item)
    return out


def merge_rows(rows: Iterable[dict], *, merge_seconds: int) -> list[dict]:
    """同一个人连续发言合并成一条（间隔 ≤ merge_seconds）。

    通知群里「张三：这个要交 / 张三：明天截止」拆成两条，模型容易只看到后半句。
    """
    gap = int(merge_seconds or 0)
    out: list[dict] = []
    for row in rows:
        text = str(row.get("text") or "").strip()
        if not text:
            continue
        last = out[-1] if out else None
        if (gap > 0 and last is not None
                and str(last.get("sender")) == str(row.get("sender"))
                and int(row.get("ts") or 0) - int(last.get("ts") or 0) <= gap):
            last["text"] = f"{last['text']} / {text}"
            last["ts_end"] = int(row.get("ts") or 0)
            continue
        item = dict(row)
        item["ts_end"] = int(row.get("ts") or 0)
        out.append(item)
    return out


def format_line(row: dict) -> str:
    stamp = time.strftime("%m-%d %H:%M", time.localtime(int(row.get("ts") or 0)))
    who = str(row.get("sender") or "").strip() or str(row.get("user_id") or "?")
    return f"[{stamp}] {who}: {row.get('text')}".strip()


def fit_budget(items: list[dict], max_chars: int) -> tuple[list[dict], bool]:
    """超出字数预算就**均匀抽稀**（保留时间分布的形态），返回 `(保留项, 是否抽稀过)`。"""
    if not items:
        return [], False
    total = sum(len(item["line"]) for item in items)
    if total <= max_chars or max_chars <= 0:
        return items, False
    avg = max(1, total // len(items))
    keep_n = max(8, int(max_chars // avg))
    if keep_n >= len(items):
        keep_n = len(items)
    stride = len(items) / keep_n
    picked, seen = [], set()
    for i in range(keep_n):
        item = items[min(len(items) - 1, int(i * stride))]
        if id(item) in seen:
            continue
        seen.add(id(item))
        picked.append(item)
    while len(picked) > 1 and sum(len(x["line"]) for x in picked) > max_chars:
        picked.pop()
    return picked, True


def build(rows: Iterable[dict], *, clean_cfg: dict, max_chars: int) -> dict:
    """清洗 + 合并 + 裁剪，一次拿到「给模型的文本」和「统计口径」。

    Returns:
        `{"lines": [...], "text": "...", "raw": 原始条数, "kept": 保留条数,
          "sampled": 是否抽稀过}`
    """
    rows = list(rows)
    cleaned = clean_rows(rows, clean_cfg)
    merged = merge_rows(cleaned, merge_seconds=int(clean_cfg.get("merge_seconds") or 0))
    items = [{"row": row, "line": format_line(row)} for row in merged]
    items, sampled = fit_budget(items, int(max_chars))
    lines = [item["line"] for item in items]
    return {
        "lines": lines,
        "text": "\n".join(lines),
        "raw": len(rows),
        "kept": len(lines),
        "sampled": sampled,
    }


SYSTEM_PROMPT = """你是通知群消息整理助手。用户会给你一段 QQ 群聊天记录（可能带[@某某]与[图片]/[文件]占位），
你要从中抽出「需要有人去做的事」。

规则：
1. 只抽**有明确行动要求**的事项：报名、提交、截止、开会、领取、缴费、确认回复、填表、打卡等；
   纯通知（已结束的、无需行动的）、闲聊、情绪表达都不算。
2. 同一件事重复出现（多人复述、催办）合并成**一条**。
3. `owner` 只填记录里**明确**提到的责任人（@某人、某部门、某班、某组）；没提到就留空字符串，不要猜。
4. `due` 填绝对时间 `YYYY-MM-DD HH:MM`；只有「本周五」「明晚」这类相对说法时按记录里的日期换算；
   没提到就留空字符串。
5. `title` 不超过 30 个字，动宾结构，写「做什么」而不是「谁说了什么」。
6. `priority` 取 high / normal / low：临近截止、明确要求当天完成的算 high。
7. `evidence` 抄一句原始消息里最关键的话（不超过 40 字），便于溯源。
8. 最多输出 {max_todos} 条，按紧急程度从高到低排。

只输出 JSON，不要解释，不要 markdown 代码围栏：
{"todos":[{"title":"","owner":"","due":"","priority":"normal","evidence":""}]}

如果确实没有任何待办，输出 {"todos":[]}。"""


def build_chat_messages(*, group_name: str, digest: dict,
                        start_ts: int, end_ts: int, max_todos: int) -> list[dict]:
    """拼成 OpenAI 兼容的 `messages`。"""
    span = (f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(start_ts))} ~ "
            f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(end_ts))}")
    head = [
        f"群名称：{group_name or '未知'}",
        f"时间范围：{span}",
        f"消息情况：原始 {digest['raw']} 条，去噪合并后 {digest['kept']} 条"
        + ("（消息过多，已按时间均匀抽样）" if digest.get("sampled") else ""),
        "以下是聊天记录：",
    ]
    user = "\n".join(head) + "\n\n" + digest["text"]
    return [
        {"role": "system", "content": SYSTEM_PROMPT.replace("{max_todos}", str(max_todos))},
        {"role": "user", "content": user},
    ]


__all__ = [
    "is_noise", "clean_rows", "merge_rows", "format_line", "fit_budget",
    "build", "build_chat_messages", "SYSTEM_PROMPT",
]
