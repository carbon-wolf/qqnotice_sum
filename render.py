# -*- coding: utf-8 -*-
"""文案渲染：面板 / 待办列表 / 总结正文 / 设置 / 状态。

一条原则：**所有数字都从库里现查**，不缓存、不猜。面板说「采集 128 条」就必须
真的是 128 条 —— 用户对不上就会怀疑整个插件。
"""
from __future__ import annotations

import time

from . import store

PRIORITY_TAG = {"high": "[高]", "normal": "", "low": "[低]"}
STATUS_LABEL = {"open": "未完成", "done": "已完成", "dropped": "已忽略"}


def clock(ts: int, fmt: str = "%m-%d %H:%M") -> str:
    try:
        return time.strftime(fmt, time.localtime(int(ts)))
    except (TypeError, ValueError, OSError):
        return "?"


def _disp_width(text: str) -> int:
    """粗略显示宽度：CJK 全角算 2，其余算 1（只为对齐，不追求精确）。"""
    return sum(2 if ord(ch) > 0x2E7F else 1 for ch in text)


def _pad(text: str, width: int) -> str:
    """按**显示宽度**补空格 —— `str.ljust` 按字符数补，中文行会参差不齐。"""
    return text + " " * max(0, width - _disp_width(text))


def span_text(start_ts: int, end_ts: int) -> str:
    return f"{clock(start_ts)} ~ {clock(end_ts)}"


def group_title(plugin, group_id) -> str:
    """群显示名：配置里存的名字 > 群号。"""
    cfg = plugin.group_cfg(group_id)
    name = str(cfg.get("name") or "").strip()
    return f"{name}（{group_id}）" if name else str(group_id)


def _todo_line(todo: dict) -> str:
    tag = PRIORITY_TAG.get(str(todo.get("priority") or "normal"), "")
    head = f"#{todo.get('id')} {tag}{str(todo.get('title') or '').strip()}".replace("  ", " ")
    bits = []
    if str(todo.get("owner") or "").strip():
        bits.append("@" + str(todo["owner"]).strip())
    if str(todo.get("due") or "").strip():
        bits.append("截止 " + str(todo["due"]).strip())
    return head + ("\n   " + " · ".join(bits) if bits else "")


# ------------------------------------------------------------------ 面板

USAGE_LINES = [
    ("/sum", "这个面板"),
    ("/sum summary [24h]", "现在总结一次；窗口可写 24h / 12h / today / yesterday / 3d"),
    ("/sum list [all]", "未完成的待办（带 all 连已完成的一起列）"),
    ("/sum done <编号>", "标记完成，编号是列表里的 #数字"),
    ("/sum drop <编号>", "忽略某条（以后不再重复提醒）"),
    ("/sum sync [24h]", "强制从群历史补采一次"),
    ("/sum config …", "群配置：add / del / window / time / push / model / switch"),
    ("/sum status", "采集与模型状态"),
]


def panel(plugin) -> str:
    """概览面板（私聊用）。"""
    lines = [f"📋 qqnotice_sum · v{plugin.version}"]
    gids = sorted(plugin.group_ids())
    if not gids:
        lines.append("")
        lines.append("还没有托管任何群。先加一个：`/sum config add <群号>`")
    else:
        lines.append("")
        lines.append(f"托管 {len(gids)} 个群：")
        for gid in gids:
            open_count = len(store.open_todos(gid))
            total = store.message_count(gid)
            push_time = plugin.push_time(gid)
            push = f"每日 {push_time} 推送" if (plugin.push_on(gid) and push_time) else "不自动推送"
            lines.append(f"  · {group_title(plugin, gid)}")
            lines.append(f"    {open_count} 条未完成 · {push} · 累计 {total} 条消息")
            if not plugin.group_cfg(gid).get("enabled", True):
                lines.append("    ⏸ 采集已暂停（/sum config switch on）")

    lines.append("")
    lines.append("用法（私聊发；只有这一条命令）")
    for usage, desc in USAGE_LINES:
        lines.append("  " + _pad(usage, 26) + desc)
    return "\n".join(lines)


# ------------------------------------------------------------------ 总结

def summary(plugin, group_id, *, start_ts: int, end_ts: int, digest: dict,
            todos: list[dict], model: str) -> str:
    lines = [f"📋 {group_title(plugin, group_id)} · {span_text(start_ts, end_ts)}"]
    stat = f"消息 {digest['raw']} 条（去噪合并后 {digest['kept']} 条）"
    if digest.get("sampled"):
        stat += " · 已抽样"
    lines.append(stat + (f" · {model}" if model else ""))

    if todos:
        lines.append("")
        for idx, todo in enumerate(todos, 1):
            tag = PRIORITY_TAG.get(str(todo.get("priority") or "normal"), "")
            bits = []
            if str(todo.get("owner") or "").strip():
                bits.append("@" + str(todo["owner"]).strip())
            if str(todo.get("due") or "").strip():
                bits.append("截止 " + str(todo["due"]).strip())
            lines.append(f"{idx}. {tag}{todo['title']}")
            if bits:
                lines.append("   " + " · ".join(bits))
    else:
        lines.append("")
        lines.append("这段时间没有「需要有人去做」的事项。")
    return "\n".join(lines)


def unstructured(raw: str, *, digest: dict, start_ts: int, end_ts: int) -> str:
    """模型没按 JSON 输出时的兜底：把原文当概览回，明确标注未结构化。"""
    return (
        f"📋 {span_text(start_ts, end_ts)} · 消息 {digest.get('raw', 0)} 条\n"
        "⚠️ 模型这次没按格式输出，下面是原文（未写入待办库）：\n\n"
        f"{raw.strip()[:1500]}"
    )


_EMPTY_NO_MESSAGES = (
    "📭 {span} 这段时间没采集到消息。\n"
    "可能原因：插件刚装（还没开始收）、机器人当时不在线，"
    "或该群不在框架的监听范围里（config.json 的 bound_groups）。\n"
    "可以试 `/sum sync` 从群历史里捞一次。"
)
_EMPTY_NOISE = "📭 {span} 的消息都是「收到/好的」这类没有信息量的内容，没什么可总结的。"


def result(plugin, group_id, res: dict) -> str:
    """把 `service.summarize()` 的结果渲染成一条消息。"""
    start_ts = int(res.get("start_ts") or 0)
    end_ts = int(res.get("end_ts") or 0)
    if res.get("empty"):
        template = (_EMPTY_NO_MESSAGES if res.get("reason") == "no_messages"
                    else _EMPTY_NOISE)
        return template.format(span=span_text(start_ts, end_ts))
    if not res.get("structured"):
        return unstructured(res.get("raw") or "", digest=res.get("digest") or {},
                            start_ts=start_ts, end_ts=end_ts)
    return summary(plugin, group_id, start_ts=start_ts, end_ts=end_ts,
                   digest=res.get("digest") or {}, todos=res.get("todos") or [],
                   model=res.get("model") or "")


# ------------------------------------------------------------------ 列表

def todo_list(plugin, group_id, *, include_closed: bool = False) -> str:
    rows = store.open_todos(group_id)
    closed = []
    if include_closed:
        closed = [r.to_dict() for r in
                  store.Todo.objects.filter(group_id=int(group_id)).order_by("-id").limit(30)
                  if r.status != "open"]
    lines = [f"📋 {group_title(plugin, group_id)} · 待办清单"]
    if not rows and not closed:
        return "\n".join(lines + ["", "还没有待办。先发 `/sum summary` 生成一份。"])
    if rows:
        lines.append("")
        lines.append(f"未完成 {len(rows)} 条：")
        for todo in rows:
            lines.append(_todo_line(todo))
    if closed:
        lines.append("")
        lines.append("最近处理过：")
        for todo in closed[:10]:
            label = STATUS_LABEL.get(str(todo.get("status")), "")
            lines.append(f"#{todo.get('id')} {label} {str(todo.get('title') or '').strip()}")
    lines.append("")
    lines.append("标记完成：/sum done <编号>　忽略：/sum drop <编号>")
    return "\n".join(lines)


def closed_ack(todo: dict, status: str) -> str:
    label = STATUS_LABEL.get(status, "已处理")
    owner = str(todo.get("owner") or "").strip()
    tail = f"（@{owner}）" if owner else ""
    return f"✅ #{todo.get('id')} {label}：{str(todo.get('title') or '').strip()}{tail}"


# ------------------------------------------------------------------ 设置

def config_overview(plugin) -> str:
    """不带群号时的配置总览。"""
    defaults = plugin.defaults()
    lines = [
        "⚙️ 配置",
        "",
        "用法：`/sum config <项> [群号] <值>`（只托管一个群时群号可省）",
        "",
        "  add <群号>            托管一个群",
        "  del <群号>            取消托管（数据保留）",
        "  window 24h            总结默认看多久（12h / today / yesterday / 3d）",
        "  time 21:30            每天几点自动推送（off = 不定时）",
        "  push on|off           该群是否自动推送",
        "  model <名字>          该群用哪个模型（default = 跟随全局）",
        "  switch on|off         该群是否继续采集",
        "  default …             改全局默认（仅超管）",
        "",
        f"全局默认：window {defaults['window_hours']}h · "
        f"push {defaults['push_time'] or '关'} · max {defaults['max_todos']} 条",
    ]
    gids = sorted(plugin.group_ids())
    if gids:
        lines.append("")
        lines.append(f"已托管 {len(gids)} 个群：" + "、".join(str(g) for g in gids))
    return "\n".join(lines)


def settings(plugin, group_id) -> str:
    cfg = plugin.group_cfg(group_id)
    defaults = plugin.defaults()
    lines = [f"⚙️ {group_title(plugin, group_id)}"]
    if not cfg:
        lines.append("（这个群还没托管：/sum config add）")
        return "\n".join(lines)
    lines.append(f"  采集：{'开' if plugin.is_watched(group_id) else '关'}")
    lines.append(f"  总结窗口：{plugin.window_hours(group_id)} 小时")
    push_time = plugin.push_time(group_id)
    lines.append(f"  每日推送：{'开 · ' + push_time if push_time else '关'}")
    lines.append(f"  单次最多：{plugin.max_todos(group_id)} 条待办")
    lines.append(f"  模型：{plugin.model_for(group_id) or '跟随全局 ' + plugin.llm_cfg()['model']}")
    lines.append("")
    lines.append("修改：")
    lines.append("  /sum config window 24h　/sum config time 21:30　/sum config push off")
    lines.append("  /sum config model deepseek-chat　/sum config switch off　/sum config del")
    lines.append(f"  （全局默认：window {defaults['window_hours']}h · "
                 f"push {defaults['push_time'] or '关'}）")
    return "\n".join(lines)


def status(plugin) -> str:
    gids = sorted(plugin.group_ids())
    cfg = plugin.llm_cfg()
    now = int(time.time())
    lines = ["🩺 qqnotice_sum 状态", ""]
    lines.append(f"  版本：v{plugin.version}　托管群：{len(gids)} 个")
    lines.append(f"  模型：{cfg['model']} @ {cfg['base_url']}"
                 + ("" if cfg["api_key"] else "（⚠️ 没配 api_key）"))
    lines.append(f"  数据库：{_db_hint(plugin)}")
    lines.append("")
    if not gids:
        lines.append("  （还没有托管群：/sum config add <群号>）")
    for gid in gids:
        state = store.sync_state(gid)
        total = store.message_count(gid)
        open_count = len(store.open_todos(gid))
        last = clock(state.last_sync) if state is not None and state.last_sync else "从未"
        lines.append(f"  · {group_title(plugin, gid)}")
        lines.append(f"    消息 {total} 条 · 未完成待办 {open_count} 条 · 上次同步 {last}")
        lines.append(f"    推送 {plugin.push_time(gid) or '关'} · "
                     f"采集{'开' if plugin.is_watched(gid) else '关'} · "
                     f"窗口 {plugin.window_hours(gid)}h")
    lines.append("")
    lines.append(f"  当前时间：{clock(now, '%Y-%m-%d %H:%M')}")
    return "\n".join(lines)


def _db_hint(plugin) -> str:
    """库文件路径 —— 就是框架 `plugin.database()` 给的那个，插件没另建库。"""
    try:
        db = plugin.database()
        return str(getattr(db, "path", "") or "（框架 sqlite）")
    except Exception:                      # noqa: BLE001 - 只用于展示
        return "（框架 sqlite）"


__all__ = [
    "clock", "span_text", "group_title", "panel", "summary", "unstructured", "result",
    "todo_list", "closed_ack", "settings", "config_overview", "status",
    "PRIORITY_TAG", "STATUS_LABEL",
]
