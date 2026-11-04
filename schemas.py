# -*- coding: utf-8 -*-
"""命令参数与输入解析。

本插件**只注册一条命令** `/sum`，没有子命令 —— 所以「动作」和「参数」全在
同一串 `spec` 里，由本模块解析。这样做的原因：

* bot 上插件很多，顶层命令空间要省着用；
* 帮助只有一页（`/help sum`），不用在七八个子命令页之间跳；
* 框架里「带子命令的命令 = 命名空间、本体不执行」，注册子命令反而多一层跳转。

`/sum 看 24h` 之所以能进来，是因为**没有子命令**时解析器会把第二个词回填进
`args`（`napcat/command.py` 的 `CommandHandler.execute`）。
"""
from __future__ import annotations

import re
import time
from datetime import datetime
from typing import List, Optional, Tuple

from napcat.command import CommandSchema, VariadicField


class SumSchema(CommandSchema):
    """`/sum [action] [args ...]`"""

    spec = VariadicField(
        description="action + args：留空=面板；summary [窗口] [all|digest|todo] / "
                    "digest [窗口] / todo [窗口] / list [all] / done <编号> / "
                    "drop <编号> / sync [窗口] / config … / status")


#: 动作别名 → 内部动作名（英文为主，中文是别名）
ACTION_ALIASES = {
    "summary": "summary", "sum": "summary", "s": "summary", "report": "summary",
    "看": "summary", "总结": "summary", "看看": "summary",
    "digest": "digest", "abstract": "digest", "摘要": "digest", "内容": "digest",
    "todo": "todo", "todos": "todo", "待办": "todo",
    "list": "list", "ls": "list", "l": "list",
    "列表": "list", "清单": "list",
    "done": "done", "d": "done", "完": "done", "完成": "done",
    "drop": "drop", "弃": "drop", "忽略": "drop",
    "sync": "sync", "补": "sync", "补采": "sync",
    "ask": "ask", "q": "ask", "question": "ask", "qa": "ask",
    "问": "ask", "提问": "ask", "问问": "ask", "问一下": "ask",
    "config": "config", "cfg": "config", "set": "config",
    "设置": "config", "配置": "config",
    "status": "status", "状态": "status",
    "help": "help", "帮助": "help", "?": "help",
}

#: 产出模式别名
MODE_ALIASES = {
    "all": "all", "both": "all", "全部": "all", "都要": "all", "综合": "all",
    "digest": "digest", "summary": "digest", "摘要": "digest", "内容": "digest",
    "todo": "todo", "todos": "todo", "待办": "todo", "任务": "todo",
}


def parse_mode(text, default: str = "all") -> str:
    """产出模式：`all` / `digest` / `todo`；认不出来返回默认。"""
    return MODE_ALIASES.get(str(text or "").strip().lower(), default)

#: 窗口单位 → 秒
_UNITS = {
    "分钟": 60, "分": 60, "min": 60, "mins": 60, "m": 60,
    "小时": 3600, "时": 3600, "h": 3600, "hr": 3600, "hrs": 3600,
    "hour": 3600, "hours": 3600,
    "天": 86400, "d": 86400, "day": 86400, "days": 86400,
}

#: 长的单位写前面，避免 `小时` 被 `时` 抢走
_PART_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(分钟|小时|分|时|天|mins|min|hrs|hr|hours|hour|days|day|h|d|m)")

#: 窗口上限：再长也没意义，而且会拖垮接口
MAX_WINDOW_SECONDS = 90 * 86400


def tokenize(spec) -> List[str]:
    """`VariadicField` 给的是 `list[str]`，但也容忍单个字符串。"""
    if spec is None:
        return []
    if isinstance(spec, str):
        return [spec.strip()] if spec.strip() else []
    return [str(x).strip() for x in spec if str(x).strip()]


def parse_spec(spec) -> Tuple[str, List[str]]:
    """`spec` → `(动作, 其余参数)`。空 = `panel`；不认识的动词 = `unknown`。"""
    tokens = tokenize(spec)
    if not tokens:
        return "panel", []
    head = tokens[0].lower()
    return ACTION_ALIASES.get(head, "unknown"), tokens[1:]


def parse_hhmm(text) -> Optional[str]:
    """`21:30` / `9:5` / `2130` → `"21:30"`；`关` / `off` → `""`；非法 → None。"""
    raw = str(text or "").strip()
    if not raw:
        return None
    if raw in ("关", "关闭", "off", "none", "0"):
        return ""
    if raw.isdigit() and len(raw) in (3, 4):
        raw = ("0" + raw if len(raw) == 3 else raw)
        raw = raw[:-2] + ":" + raw[-2:]
    try:
        hour_text, minute_text = raw.split(":")
        hour, minute = int(hour_text), int(minute_text)
    except (ValueError, AttributeError):
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return f"{hour:02d}:{minute:02d}"


def parse_bool(text, default: Optional[bool] = None) -> Optional[bool]:
    """开/关类词。`on/off/是/否/true/false/1/0`。"""
    raw = str(text or "").strip().lower()
    if not raw:
        return default
    if raw in ("开", "on", "true", "1", "yes", "是", "启用", "开启"):
        return True
    if raw in ("关", "off", "false", "0", "no", "否", "停用", "关闭"):
        return False
    return default


def parse_number(text, *, minimum: int = 1, maximum: int = 100000) -> Optional[int]:
    try:
        num = int(str(text).strip())
    except (TypeError, ValueError):
        return None
    return num if minimum <= num <= maximum else None


def parse_group_id(text) -> Optional[int]:
    """群号解析：去掉 `#` / `@` / 括号，只认 5 位以上的数字。"""
    raw = str(text or "").strip().lstrip("#")
    if not raw:
        return None
    raw = raw.strip("<>[]()（）").lstrip("@")
    if not raw.isdigit():
        return None
    gid = int(raw)
    return gid if gid > 10000 else None


def _day_start(stamp: int) -> int:
    dt = datetime.fromtimestamp(stamp)
    return int(datetime(dt.year, dt.month, dt.day).timestamp())


def parse_window(text, *, now: Optional[int] = None) -> Optional[Tuple[int, int]]:
    """窗口 → `(start_ts, end_ts)`；认不出来返回 None（调用方回落到默认窗口）。

    支持的写法：`24h` / `12h` / `90m` / `3d` / `1d12h` / `today` / `yesterday` /
    `week` / `all`，以及对应的中文：今天 / 昨天 / 本周 / 全部。
    """
    raw = str(text or "").strip().lower()
    if not raw:
        return None
    stamp = int(now if now is not None else time.time())

    if raw in ("今天", "今日", "today"):
        return _day_start(stamp), stamp
    if raw in ("昨天", "昨日", "yesterday"):
        start = _day_start(stamp) - 86400
        return start, start + 86400
    if raw in ("本周", "这周", "week", "thisweek"):
        start = _day_start(stamp)
        weekday = datetime.fromtimestamp(start).weekday()      # 周一 = 0
        return start - weekday * 86400, stamp
    if raw in ("全部", "所有", "all", "max"):
        return 0, stamp

    total = 0.0
    for number, unit in _PART_RE.findall(raw):
        total += float(number) * _UNITS.get(unit, 0)
    if total <= 0:
        return None
    seconds = min(int(total), MAX_WINDOW_SECONDS)
    return stamp - seconds, stamp


def window_hours(text) -> Optional[int]:
    """把窗口文本折算成小时（写配置时用）。`3d` → 72。"""
    span = parse_window(text)
    if span is None:
        return None
    seconds = max(1, span[1] - span[0])
    return max(1, int(round(seconds / 3600)))


__all__ = [
    "SumSchema", "ACTION_ALIASES", "MODE_ALIASES", "tokenize", "parse_spec",
    "parse_mode", "parse_hhmm", "parse_bool", "parse_number", "parse_group_id",
    "parse_window", "window_hours",
]
