# -*- coding: utf-8 -*-
"""唯一一条命令 `/sum` —— 所有动作都在这里分发。

三个设计约束：

1. **只有一条命令、零子命令**。bot 上插件很多，顶层命令空间要省着用；
   而且框架里「带子命令的命令」本体不执行，裸调会被重定向到 `/help`，
   反而多一层跳转。所以 `spec` 收全部剩余词，第一词当动作，其余当参数
   （`/sum summary 24h` → `("summary", ["24h"])`）。

   框架会把**没有子命令**的命令的第二个词回填进 `args`（`CommandHandler.execute`），
   中文别名因此不需要额外注册。

2. **只用私聊**：`scope="private"`。群里发不会响应（也不会在群里刷屏），
   推送由定时任务直接发，不依赖命令。

3. **权限交给框架**：`permission="admin"` + `scope="private"` 写在注册处，
   运行时可以用 `/perm set sum member` 放宽，或用 `/userperm set <QQ> admin`
   给某个人提级。插件内部不再自己判等级 —— 那会和框架的覆盖互相打架。
"""
from __future__ import annotations

import time
from typing import Optional

from napcat.message import MessageChain
from napcat.registry import command

from .. import history, render, schemas, service, store

#: 连按两次的冷却（秒）。一次总结要花 token，防手抖比防人更重要。
COOLDOWN_SECONDS = 10

#: `config` 的二级动作（英文为主，中文是别名）
CONFIG_ALIASES = {
    "add": "add", "加群": "add", "添加": "add", "加入": "add",
    "del": "del", "remove": "del", "减群": "del", "删除": "del", "移除": "del",
    "window": "window", "窗口": "window", "时长": "window",
    "time": "time", "push-time": "time", "时间": "time", "推送时间": "time",
    "push": "push", "推送": "push",
    "model": "model", "模型": "model",
    "mode": "mode", "模式": "mode", "产出": "mode",
    "switch": "switch", "enabled": "switch", "开关": "switch", "启用": "switch",
    "default": "default", "默认": "default",
}


@command("sum", schema=schemas.SumSchema, permission="admin", scope="private",
         aliases=["待办", "td"],
         description="通知群消息 → 待办：私聊查询 / 每日推送 / 群配置（仅管理员 · 仅私聊）")
async def cmd_sum(plugin, params, event):
    action, rest = schemas.parse_spec(getattr(params, "spec", None))
    handler = _ACTIONS.get(action)
    if handler is None:
        return _out(f"没听懂「{action}」。发 `/sum` 看用法。")
    return await handler(plugin, event, rest)


# ------------------------------------------------------------------ 上下文工具

def _temp_group(event) -> Optional[int]:
    """群小窗（`message_type=private` 但带 `group_id`）能认出是哪个群。"""
    if getattr(event, "message_type", "") == "group" or getattr(event, "is_temp_session", False):
        try:
            return int(getattr(event, "group_id", 0) or 0) or None
        except (TypeError, ValueError):
            return None
    return None


def _target(plugin, event, rest: list[str], *, with_window: bool = True,
            group_min_tokens: int = 1) -> tuple[Optional[int], Optional[tuple], list[str]]:
    """从参数里认出 `(群号, 时间窗口, 剩余参数)`。

    * 群小窗自带群号；私聊则看第一个词是不是群号。
    * 只托管了一个群时，群号可以省略 —— 少打十来个数字。
    * `with_window=False` 不解释窗口：否则 `/sum list all` 里的 `all` 会被
      当成窗口（它是 0~now 的合法窗口）吃掉。
    * `group_min_tokens` 防歧义：`/sum done 123456` 里的数字是**编号**不是群号，
      所以它要求至少两个词才认群号。
    """
    gid = _temp_group(event)
    tokens = list(rest)
    if gid is None and tokens and len(tokens) >= group_min_tokens:
        maybe = schemas.parse_group_id(tokens[0])
        if maybe is not None:
            gid = maybe
            tokens.pop(0)
    if gid is None:
        known = sorted(plugin.group_ids())
        if len(known) == 1:
            gid = known[0]
    span = None
    if with_window and tokens:
        span = schemas.parse_window(tokens[0])
        if span is not None:
            tokens.pop(0)
    return gid, span, tokens


def _need_group(plugin, example: str) -> str:
    known = sorted(plugin.group_ids())
    if not known:
        return "还没有托管任何群。先加一个：`/sum config add <群号>`"
    listed = "、".join(str(g) for g in known)
    return f"要指定群号：`{example}`\n已托管：{listed}"


def _out(text: str):
    """短文本直接回字符串（框架自己包消息链）；超长的按 1200 字分条。"""
    text = text or ""
    parts = MessageChain().text(text).split(max_chars=1200)
    return text if len(parts) == 1 else parts


# ------------------------------------------------------------------ 动作：面板

async def _act_panel(plugin, event, rest):
    return _out(render.panel(plugin))


async def _act_help(plugin, event, rest):
    return _out(render.panel(plugin))


# ------------------------------------------------------------------ 动作：总结

async def _act_summary(plugin, event, rest):
    return await _run_summary(plugin, event, rest, default_mode="all")


async def _act_digest(plugin, event, rest):
    return await _run_summary(plugin, event, rest, default_mode="digest")


async def _act_todo(plugin, event, rest):
    return await _run_summary(plugin, event, rest, default_mode="todo")


async def _run_summary(plugin, event, rest, *, default_mode: str):
    """`summary` / `digest` / `todo` 三个动作共用的执行体。

    参数顺序：`[群号] [窗口] [模式]`，都能省；只托管一个群时群号可省。
    """
    gid, span, tokens = _target(plugin, event, rest)
    if gid is None:
        return _out(_need_group(plugin, "/sum summary <群号> [24h] [all|digest|todo]"))
    if not plugin.is_watched(gid):
        return _out(f"群 {gid} 没有托管。加进来：`/sum config add {gid}`")

    mode = plugin.mode_for(gid) if default_mode == "all" else default_mode
    if tokens:
        mode = schemas.parse_mode(tokens[0], mode)

    now = int(time.time())
    last = plugin._last_summary.get(gid, 0)
    if now - last < COOLDOWN_SECONDS:
        return _out(f"⏳ 刚总结过，{COOLDOWN_SECONDS - (now - last)} 秒后再试。")
    plugin._last_summary[gid] = now

    if span is not None:
        start_ts, end_ts = span
    else:
        start_ts, end_ts = service.window_for(plugin, gid)

    result = await service.summarize(plugin, gid, start_ts=start_ts, end_ts=end_ts,
                                     mode=mode, catch_up_pages=3)
    return _out(render.result(plugin, gid, result))


# ------------------------------------------------------------------ 动作：清单

async def _act_list(plugin, event, rest):
    gid, _span, tokens = _target(plugin, event, rest, with_window=False)
    if gid is None:
        return _out(_need_group(plugin, "/sum list <群号>"))
    include_closed = any(t.lower() in ("all", "全部", "全") for t in tokens)
    return _out(render.todo_list(plugin, gid, include_closed=include_closed))


async def _act_done(plugin, event, rest):
    return await _close_action(plugin, event, rest, status="done")


async def _act_drop(plugin, event, rest):
    return await _close_action(plugin, event, rest, status="dropped")


async def _close_action(plugin, event, rest, *, status: str):
    verb = "done" if status == "done" else "drop"
    gid, _span, tokens = _target(plugin, event, rest, with_window=False,
                                 group_min_tokens=2)
    if gid is None:
        return _out(_need_group(plugin, f"/sum {verb} <群号> <编号>"))
    if not tokens:
        return _out(f"用法：`/sum {verb} <编号>`，编号是 `/sum list` 里的 #数字。")
    todo_id = schemas.parse_number(tokens[-1], minimum=1, maximum=10 ** 9)
    if todo_id is None:
        return _out(f"「{tokens[-1]}」不是有效编号。用法：`/sum {verb} <编号>`。")
    row = store.get_todo(todo_id)
    if row is None or int(row.group_id) != int(gid):
        return _out(f"没找到 #{todo_id}（本群的待办编号见 `/sum list`）。")
    if row.status != "open":
        return _out(f"#{todo_id} 已经处理过了（{render.STATUS_LABEL.get(row.status, row.status)}）。")
    try:
        user_id = int(getattr(event, "user_id", 0) or 0)
    except (TypeError, ValueError):
        user_id = 0
    saved = store.close_todo(todo_id, user_id=user_id, status=status)
    return _out(render.closed_ack(saved.to_dict(), status))


# ------------------------------------------------------------------ 动作：补采

async def _act_sync(plugin, event, rest):
    gid, span, _tokens = _target(plugin, event, rest)
    if gid is None:
        return _out(_need_group(plugin, "/sum sync <群号> [24h]"))
    if not plugin.is_watched(gid):
        return _out(f"群 {gid} 没有托管：`/sum config add {gid}`")
    if span is not None:
        start_ts, end_ts = span
    else:
        start_ts, end_ts = service.window_for(plugin, gid)
    try:
        saved = await history.backfill(plugin, gid, since_ts=start_ts)
    except Exception as e:                 # noqa: BLE001 - 上游错误原样告诉管理员
        return _out(f"❌ 补采失败：{e}")
    total = store.message_count(gid)
    return _out(f"✅ 补采完成：新入库 {saved} 条（窗口 {render.span_text(start_ts, end_ts)}）。\n"
                f"本群累计 {total} 条。现在可以 `/sum summary` 了。")


# ------------------------------------------------------------------ 动作：状态

async def _act_status(plugin, event, rest):
    return _out(render.status(plugin))


# ------------------------------------------------------------------ 动作：配置

async def _act_config(plugin, event, rest):
    gid, _span, tokens = _target(plugin, event, rest, with_window=False)
    if gid is None:
        # 群号可能写在项后面：`/sum config window 987654321 12h`
        gid, tokens = _pull_group(tokens)
    if not tokens:
        if gid is None:
            return _out(render.config_overview(plugin))
        return _out(render.settings(plugin, gid))

    key = CONFIG_ALIASES.get(tokens[0].lower())
    if key is None:
        return _out(f"不认识「{tokens[0]}」。可用：add / del / window / time / "
                    "push / model / switch / default。")
    handler = _CONFIG_ACTIONS[key]
    return _out(await handler(plugin, event, gid, tokens[1:]))


def _pull_group(tokens: list[str]) -> tuple[Optional[int], list[str]]:
    """把参数里第一个「长得像群号」的词摘出来当群号。"""
    for index, token in enumerate(tokens):
        gid = schemas.parse_group_id(token)
        if gid is not None:
            return gid, tokens[:index] + tokens[index + 1:]
    return None, list(tokens)


async def _cfg_add(plugin, event, gid, args):
    gid = _first_group_id(args) or gid
    if gid is None:
        return "要指定群号：`/sum config add <群号>`"
    existed = bool(plugin.group_cfg(gid))
    plugin.update_group(gid, enabled=True)
    head = "ℹ️ 本来就在托管" if existed else "✅ 已托管"
    lines = [f"{head}：{render.group_title(plugin, gid)}",
             f"窗口 {plugin.window_hours(gid)}h · 每日推送 {plugin.push_time(gid) or '关'}"]
    bound = _framework_bound_groups(plugin)
    if bound and gid not in bound:
        lines.append("")
        lines.append(f"⚠️ 框架的 bound_groups 里没有这个群（当前只监听 {sorted(bound)}），"
                     "插件收不到它的消息。要么把它加进去，要么把 bound_groups 清空。")
    return "\n".join(lines)


async def _cfg_del(plugin, event, gid, args):
    gid = _first_group_id(args) or gid
    if gid is None:
        return "要指定群号：`/sum config del <群号>`"
    if not plugin.drop_group(gid):
        return f"群 {gid} 本来就不在托管列表里。"
    return f"🗑 已取消托管：{gid}\n（已采集的消息和待办还留着，加回来能继续用。）"


async def _cfg_window(plugin, event, gid, args):
    gid = gid or _first_group_id(args)
    if not args:
        hours = plugin.window_hours(gid) if gid else plugin.defaults()["window_hours"]
        return f"当前窗口：{hours} 小时"
    if gid is None:
        return _need_group(plugin, "/sum config window <群号> 24h")
    hours = schemas.window_hours(args[0])
    if hours is None:
        return f"「{args[0]}」不是有效窗口。写法：24h / 12h / today / 3d。"
    plugin.update_group(gid, window_hours=hours)
    return f"✅ 本群总结窗口改成 {hours} 小时。"


async def _cfg_time(plugin, event, gid, args):
    gid = gid or _first_group_id(args)
    if not args:
        current = plugin.push_time(gid) if gid else plugin.defaults()["push_time"]
        return f"当前推送时刻：{current or '关'}"
    if gid is None:
        return _need_group(plugin, "/sum config time <群号> 21:30")
    clock = schemas.parse_hhmm(args[0])
    if clock is None:
        return f"「{args[0]}」不是有效时刻。写法：21:30 或 关。"
    plugin.update_group(gid, push_time=clock, last_push_date="")
    return f"✅ 每日推送时间改成 {clock}。" if clock else "✅ 已关闭本群的每日推送。"


async def _cfg_push(plugin, event, gid, args):
    gid = gid or _first_group_id(args)
    if gid is None:
        return _need_group(plugin, "/sum config push <群号> on|off")
    value = schemas.parse_bool(args[0] if args else "")
    if value is None:
        return "用法：`/sum config push on|off`（on / off / 开 / 关）"
    plugin.update_group(gid, push=value, last_push_date="")
    return f"✅ 每日推送已{'开启' if value else '关闭'}（时刻 {plugin.push_time(gid) or '未设'}）。"


async def _cfg_model(plugin, event, gid, args):
    gid = gid or _first_group_id(args)
    if gid is None:
        return _need_group(plugin, "/sum config model <群号> deepseek-chat")
    if not args:
        return f"当前模型：{plugin.model_for(gid) or '跟随全局 ' + plugin.llm_cfg()['model']}"
    model = " ".join(args).strip()[:64]
    plugin.update_group(gid, model="" if model in ("默认", "default", "跟随") else model)
    return f"✅ 本群模型：{plugin.model_for(gid) or '跟随全局 ' + plugin.llm_cfg()['model']}"


async def _cfg_switch(plugin, event, gid, args):
    gid = gid or _first_group_id(args)
    if gid is None:
        return _need_group(plugin, "/sum config switch <群号> on|off")
    value = schemas.parse_bool(args[0] if args else "")
    if value is None:
        return "用法：`/sum config switch on|off`"
    plugin.update_group(gid, enabled=value)
    return f"✅ 本群采集已{'开启' if value else '暂停（不再采新消息，已有数据保留）'}。"


async def _cfg_default(plugin, event, gid, args):
    """改全局默认值（新托管的群照这个来）。仅超管 —— 它影响所有群。"""
    perm = getattr(plugin, "perm", None)
    try:
        user_id = int(getattr(event, "user_id", 0) or 0)
    except (TypeError, ValueError):
        user_id = 0
    if perm is not None and not perm.is_superuser(user_id):
        return "全局默认值只有超管能改。"
    if len(args) < 2:
        defaults = plugin.defaults()
        return (f"全局默认：window {defaults['window_hours']}h · "
                f"push {defaults['push_time'] or '关'} · max {defaults['max_todos']} 条\n"
                "改法：`/sum config default window 24h` / `default time 21:30` / `default max 30`")
    key, value = args[0].lower(), args[1]
    if key in ("window", "窗口"):
        hours = schemas.window_hours(value)
        if hours is None:
            return "窗口写法：24h / 12h / 3d"
        plugin.set_default("window_hours", hours)
        return f"✅ 全局默认窗口 = {hours}h"
    if key in ("time", "时间"):
        clock = schemas.parse_hhmm(value)
        if clock is None:
            return "时刻写法：21:30 或 关"
        plugin.set_default("push_time", clock)
        return f"✅ 全局默认推送时间 = {clock or '关'}"
    if key in ("max", "条数"):
        num = schemas.parse_number(value, minimum=1, maximum=200)
        if num is None:
            return "条数写 1~200 的整数"
        plugin.set_default("max_todos", num)
        return f"✅ 全局默认单次最多 {num} 条"
    return "默认值可以改：window / time / max"


async def _cfg_mode(plugin, event, gid, args):
    """该群默认产出什么：all / digest / todo。"""
    gid = gid or _first_group_id(args)
    if gid is None:
        return _need_group(plugin, "/sum config mode <群号> all|digest|todo")
    if not args:
        return f"当前模式：{plugin.mode_for(gid)}（{service.mode_label(plugin.mode_for(gid))}）"
    mode = schemas.parse_mode(args[0], "")
    if mode not in service.MODES:
        return "模式写 all / digest / todo 之一。"
    plugin.update_group(gid, mode=mode)
    return f"✅ 本群默认产出改成 {mode}（{service.mode_label(mode)}）。"


def _first_group_id(args) -> Optional[int]:
    for token in args:
        gid = schemas.parse_group_id(token)
        if gid is not None:
            return gid
    return None


def _framework_bound_groups(plugin) -> Optional[set]:
    """框架当前监听的群；`None` = 全量监听（不必警告）。

    用 `_groups`（`GroupBinding`）而不是 `plugin.groups` —— 后者是本插件自己的
    方法（返回托管群配置），名字撞了。
    """
    binding = getattr(plugin, "_groups", None)
    if binding is None:
        return None
    if getattr(binding, "all_groups", True):
        return None
    bound = set(getattr(binding, "bound", set()) or set())
    return bound or None


_ACTIONS = {
    "panel": _act_panel,
    "help": _act_help,
    "summary": _act_summary,
    "digest": _act_digest,
    "todo": _act_todo,
    "list": _act_list,
    "done": _act_done,
    "drop": _act_drop,
    "sync": _act_sync,
    "status": _act_status,
    "config": _act_config,
}

_CONFIG_ACTIONS = {
    "add": _cfg_add,
    "mode": _cfg_mode,
    "del": _cfg_del,
    "window": _cfg_window,
    "time": _cfg_time,
    "push": _cfg_push,
    "model": _cfg_model,
    "switch": _cfg_switch,
    "default": _cfg_default,
}
