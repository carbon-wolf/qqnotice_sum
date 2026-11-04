# -*- coding: utf-8 -*-
"""群消息问答：把「总结」扩展成「能追问」。

三件事：

    detect_span()      从问题里认时间（「这周有什么要交的」→ 本周窗口）
    select()           从窗口内挑出与问题相关的消息
    parse_answer()     容错解析 `{"answer","refs","enough"}`

**检索用字符二元组重叠打分**，不引第三方分词。中文群消息里
「开题报告」「教务系统」这种词，2-gram 已经够区分，而且不怕新词、
不怕缩写 —— 引个 jieba 反而是给插件加一个几十 MB 的依赖。

为什么不用 `digest.fit_budget` 那种均匀抽稀：抽稀保留的是「时间分布的形态」，
适合做摘要；问答要的是「包含答案的那几条」，均匀抽稀正好会把它们抽掉。
"""
from __future__ import annotations

import json
import re
import time
from typing import Iterable, Optional

from . import digest
from .schemas import parse_window

#: 疑问词/虚词：先摘掉再取二元组，否则「什么」「怎么」让所有消息等分
_STOP_WORDS = (
    "什么", "哪些", "哪个", "哪位", "怎么", "怎样", "为什么", "多少", "多久",
    "是否", "有没有", "请问", "帮我", "帮忙", "一下", "看看", "告诉我",
    "关于", "相关", "可以", "需要", "我们", "咱们", "群里", "大家",
    "今天", "今日", "昨天", "昨日", "本周", "这周", "最近", "现在", "目前",
)

#: 问题里出现这些词 → 自动把窗口改成对应范围（不然问「这周」只查了 24 小时）
_TIME_HINTS = (
    ("today", ("今天", "今日", "今天的")),
    ("yesterday", ("昨天", "昨日", "昨天的")),
    ("week", ("本周", "这周", "这个星期", "这星期")),
    ("all", ("全部", "所有", "一直以来")),
)

_REL_RE = re.compile(r"最近\s*(\d+)\s*(小时|天|周|个月)")


def detect_span(question: str, *, now: Optional[int] = None) -> Optional[tuple]:
    """从问题文本里猜时间范围；猜不出来返回 None（调用方用默认窗口）。

    只在问题**明确提到**时间时才改窗口 —— 「谁负责收表」不该因为
    没提时间就只看 24 小时，那由调用方的默认窗口决定。
    """
    text = str(question or "")
    if not text.strip():
        return None
    stamp = int(now if now is not None else time.time())

    match = _REL_RE.search(text)
    if match:
        num, unit = int(match.group(1)), match.group(2)
        seconds = {"小时": 3600, "天": 86400, "周": 7 * 86400, "个月": 30 * 86400}[unit]
        return stamp - num * seconds, stamp

    for key, words in _TIME_HINTS:
        if any(word in text for word in words):
            return parse_window(key, now=stamp)
    return None


# ------------------------------------------------------------------ 检索

def grams(text: str) -> set[str]:
    """字符二元组（去掉标点空白后的相邻两字）。"""
    clean = re.sub(r"[^\w\u4e00-\u9fff]+", "", str(text or "").lower())
    if len(clean) < 2:
        return {clean} if clean else set()
    return {clean[i:i + 2] for i in range(len(clean) - 1)}


def question_grams(question: str) -> set[str]:
    text = str(question or "")
    for word in _STOP_WORDS:
        text = text.replace(word, " ")
    return grams(text)


def _score(row: dict, qgrams: set[str], question: str) -> float:
    """相关度。命中的二元组越多越相关；长消息做长度惩罚。"""
    text = str(row.get("text") or "")
    if not text:
        return 0.0
    g = grams(text)
    if not g:
        return 0.0
    hit = len(qgrams & g)
    if not hit:
        return 0.0
    score = hit / (len(g) ** 0.5)
    # 「班长说的那个表」——问题里点了名，优先看这个人发的
    who = str(row.get("sender") or "").strip()
    if who and who in question:
        score += 1.5
    return score


def select(rows: Iterable[dict], question: str, *, clean_cfg: dict,
           max_chars: int = 10000, max_lines: int = 200) -> dict:
    """挑出要喂给模型的消息。

    放得下就全给（问答最怕漏掉关键那条），放不下才按相关度筛。
    """
    merge_seconds = clean_cfg.get("merge_seconds")
    cleaned = digest.merge_rows(
        digest.clean_rows(rows, clean_cfg),
        merge_seconds=300 if merge_seconds is None else int(merge_seconds))
    total = len(cleaned)
    if not total:
        return {"items": [], "text": "", "total": 0, "picked": 0, "trimmed": False}

    numbered = []
    for index, row in enumerate(cleaned, 1):
        item = dict(row)
        item["no"] = index
        item["line"] = f"[{index}] {digest.format_line(row)}"
        numbered.append(item)

    fits = (total <= max_lines
            and sum(len(item["line"]) for item in numbered) <= max_chars)
    if fits:
        picked = numbered
    else:
        qgrams = question_grams(question)
        # 同分时新的优先：预算不够时「从旧往新填」会把刚发的通知挤掉
        ranked = sorted(numbered, key=lambda r: (-_score(r, qgrams, question),
                                                 -int(r.get("ts") or 0)))
        picked, used = [], 0
        for row in ranked:
            if len(picked) >= max_lines:
                break
            if used + len(row["line"]) > max_chars:
                continue
            picked.append(row)
            used += len(row["line"])
        # 筛完再按时间排回去，模型读起来才像一段对话
        picked.sort(key=lambda r: int(r.get("ts") or 0))

    return {
        "items": picked,
        "text": "\n".join(item["line"] for item in picked),
        "total": total,
        "picked": len(picked),
        "trimmed": len(picked) < total,
    }


# ------------------------------------------------------------------ prompt

SYSTEM_PROMPT = """你是 QQ 群消息的问答助手。用户会给你一段群聊天记录（每条前面有 [编号]， \
可能含 [图N]/[文件N] 占位），然后提一个问题。

规则：
1. 只根据记录回答。记录里没有就直说「记录里没提到」，不要用常识补，也不要猜。
2. 回答简短：先给结论，最多 3 句。需要罗列时用「- 」开头，最多 5 条。
3. 把依据的消息编号放进 refs（最多 5 个）；没有明确依据就留空数组。
4. 相对时间（「明天」「下周一」）按记录里的日期换算成绝对日期再答。
5. 问的是「谁 / 什么时候 / 在哪 / 多少钱」这类事实，就直接给答案，别复述整段通知。

只输出 JSON，不要解释，不要 markdown 代码围栏：
{"answer":"...","refs":[3,7],"enough":true}

记录不足以回答时 enough 为 false，并在 answer 里说明还缺什么。"""


def build_messages(*, group_name: str, question: str, selected: dict,
                   start_ts: int, end_ts: int) -> list[dict]:
    span = (f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(start_ts))} ~ "
            f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(end_ts))}")
    head = [
        f"群名称：{group_name or '未知'}",
        f"记录范围：{span}",
        f"记录条数：{selected['picked']} 条"
        + (f"（该时段共 {selected['total']} 条，已按问题筛掉无关的）"
           if selected["trimmed"] else ""),
        "以下是聊天记录：",
    ]
    user = ("\n".join(head) + "\n\n" + selected["text"]
            + f"\n\n问题：{question}")
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


# ------------------------------------------------------------------ 解析

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.M)


def strip_fence(text: str) -> str:
    """去掉 markdown 代码围栏（模型爱包一层，直接显示会很丑）。"""
    return _FENCE_RE.sub("", str(text or "")).strip()


def parse_answer(text: str) -> Optional[dict]:
    """抠出 `{"answer","refs","enough"}`；不是 JSON 返回 None。

    None 不等于失败 —— 上层会把原文直接当答案显示（问答场景纯文本也读得懂），
    只是没有引用而已。
    """
    raw = str(text or "").strip()
    if not raw:
        return None

    candidates = [strip_fence(raw)]
    start, end = raw.find("{"), raw.rfind("}")
    if start >= 0 and end > start:
        candidates.append(raw[start:end + 1])

    for chunk in candidates:
        try:
            data = json.loads(chunk)
        except (TypeError, ValueError):
            continue
        if isinstance(data, dict) and ("answer" in data or "enough" in data):
            return _normalize(data)
    return None


def _normalize(data: dict) -> dict:
    answer = str(data.get("answer") or data.get("回答") or "").strip()
    refs: list[int] = []
    for item in data.get("refs") or data.get("sources") or []:
        try:
            num = int(item)
        except (TypeError, ValueError):
            continue
        if num > 0 and num not in refs:
            refs.append(num)
    enough = data.get("enough")
    return {
        "answer": answer[:1500],
        "refs": refs[:8],
        "enough": True if enough is None else bool(enough),
    }


def cite_items(selected: dict, refs: Iterable[int]) -> list[dict]:
    """把编号换成可读的引用（谁、什么时候、说了什么）。"""
    by_no = {item["no"]: item for item in selected.get("items") or []}
    out = []
    for num in refs:
        item = by_no.get(int(num))
        if item is None:
            continue
        out.append({
            "no": item["no"],
            "sender": str(item.get("sender") or item.get("user_id") or "?"),
            "ts": int(item.get("ts") or 0),
            "text": str(item.get("text") or "")[:80],
        })
    return out


__all__ = [
    "detect_span", "grams", "question_grams", "select", "build_messages",
    "parse_answer", "cite_items", "strip_fence", "SYSTEM_PROMPT",
]
