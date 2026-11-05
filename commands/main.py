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
from urllib.parse import urlsplit, urlunsplit

from napcat.message import MessageChain
from napcat.registry import command

from .. import history, llm, qa, render, schemas, service, store

#: 连按两次的冷却（秒）。一次总结要花 token，防手抖比防人更重要。
COOLDOWN_SECONDS = 10

#: 这些配置项是**全局**的（不针对某个群），走单独分支，不参与群号解析。
GLOBAL_ITEMS = {"key", "api", "test", "incremental"}

#: `api clear` 恢复到这个地址
DEFAULT_BASE_URL = "https://api.deepseek.com/v1"

#: `config` 的二级动作（英文为主，中文是别名）
CONFIG_ALIASES = {
    "add": "add", "加群": "add", "添加": "add", "加入": "add",
    "del": "del", "remove": "del", "减群": "del", "删除": "del", "移除": "del",
    "window": "window", "窗口": "window", "时长": "window",
    "time": "time", "push-time": "time", "时间": "time", "推送时间": "time",
    "push": "push", "推送": "push",
    "model": "model", "模型": "model",
    "mode": "mode", "模式": "mode", "产出": "mode",
    "incremental": "incremental", "delta": "incremental",
    "增量": "incremental", "接续": "incremental",
    "name": "name", "rename": "name", "名称": "name", "群名": "name", "改名": "name",
    "persona": "persona", "role": "persona", "identity": "persona",
    "角色": "persona", "身份": "persona", "视角": "persona",
    "switch": "switch", "enabled": "switch", "开关": "switch", "启用": "switch",
    "default": "default", "默认": "default",
    "key": "key", "apikey": "key", "api-key": "key", "密钥": "key", "秘钥": "key",
    "api": "api", "base": "api", "base_url": "api", "url": "api",
    "地址": "api", "接口": "api",
    "test": "test", "测": "test", "测试": "test", "verify": "test", "check": "test",
}


@command("sum", schema=schemas.SumSchema, permission="admin", scope="private",
         aliases=["待办", "td"],
         description="通知群消息 → 待办：私聊查询 / 每日推送 / 群配置（仅管理员 · 仅私聊）")
async def cmd_sum(plugin, params, event):
    tokens = schemas.tokenize(getattr(params, "spec", None))
    action, rest = schemas.parse_spec(tokens)
    if action == "unknown":
        # 认不出的第一个词直接当问题：`/sum 谁负责收表` 比记动作名自然。
        # 拼错动作名时也只是「答一句记录里没提到」，不会做错事。
        return await _act_ask(plugin, event, tokens)
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
        if maybe is None:
            # 群名也认：「/sum summary 计院 24h」
            maybe = _match_group_name(plugin, tokens[0])
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


def _match_group_name(plugin, token) -> Optional[int]:
    """按群名（或名字片段）找群，只认唯一命中。

    「计院」能匹配到「计院26级通知群」。歧义时返回 None —— 上层会列出
    全部群让它重说，比猜错强。
    """
    key = str(token or "").strip()
    if len(key) < 2:
        return None                       # 一个字太容易撞
    if schemas.parse_group_id(key) is not None:
        return None                       # 纯数字是群号，不走这里
    hits = []
    for gid in sorted(plugin.group_ids()):
        name = str(plugin.group_cfg(gid).get("name") or "").strip()
        if not name:
            continue
        if key in name or (len(name) >= 2 and name in key):
            hits.append(gid)
    return hits[0] if len(hits) == 1 else None


def _need_group(plugin, example: str) -> str:
    known = sorted(plugin.group_ids())
    if not known:
        return "还没有托管任何群。先加一个：`/sum config add <群号>`"
    # 示例里统一标成「群名或群号」，省得以为只能报数字
    example = example.replace("<群号>", "<群名或群号>")
    lines = [f"要指定是哪个群：`{example}`", "", "已托管："]
    for gid in known[:10]:
        lines.append(f"  {render.group_title(plugin, gid)}")
    if len(known) > 10:
        lines.append(f"  …还有 {len(known) - 10} 个")
    lines.append("")
    lines.append("群号或群名都行，群名可以只写几个字（如「计院」）。")
    return "\n".join(lines)


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
        # 显式给了窗口就按给的来，不掺增量
        plan = {"start_ts": span[0], "end_ts": span[1],
                "incremental": False, "since_ts": 0, "capped": False}
    else:
        plan = service.plan_window(plugin, gid)

    result = await service.summarize(plugin, gid,
                                     start_ts=plan["start_ts"], end_ts=plan["end_ts"],
                                     window=plan, mode=mode, catch_up_pages=3)
    return _out(render.result(plugin, gid, result))


# ------------------------------------------------------------------ 动作：提问

#: 追问的冷却（秒）。比总结短得多 —— 连着问「那谁负责」「什么时候截止」是正常用法。
ASK_COOLDOWN = 3


async def _act_ask(plugin, event, rest):
    """就群里的内容提问。只读：不产生待办，也不动总结的冷却。"""
    gid, span, tokens = _target(plugin, event, rest)
    question = " ".join(tokens).strip()
    if not question:
        return _out(
            "想问什么？比如：\n"
            "  `/sum ask 这周有什么要交的`\n"
            "  `/sum ask 谁负责收表`\n"
            "  `/sum ask 开会定在几点`\n"
            "\n"
            "时间词可以直接写进问题（今天 / 昨天 / 本周 / 最近3天），"
            "也可以放在前面：`/sum ask 7d 上次发的课表在哪`。\n"
            "看的是**文字消息**（图片和附件不解析），"
            "太久远的先 `/sum sync 30d` 补一下。")
    if gid is None:
        return _out(_need_group(plugin, "/sum ask <群号> <问题>"))
    if not plugin.is_watched(gid):
        return _out(f"群 {gid} 没有托管：`/sum config add {gid}`")

    now = int(time.time())
    last = plugin._last_ask.get(gid, 0)
    if now - last < ASK_COOLDOWN:
        return _out(f"⏳ 上一个问题还在处理，{ASK_COOLDOWN - (now - last)} 秒后再问。")
    plugin._last_ask[gid] = now

    # 问题里带了「本周」「最近 3 天」就按它来，否则用固定回溯窗口。
    # 注意这里**不用** window_for：那是增量窗口，对提问没意义
    # （刚总结完再问，就只剩三分钟可看了）。
    if span is None:
        span = qa.detect_span(question, now=now)
    start_ts, end_ts = span if span is not None else service.window_full(plugin, gid)

    result = await service.ask(plugin, gid, question=question,
                               start_ts=start_ts, end_ts=end_ts,
                               model=plugin.model_for(gid), catch_up_pages=2)
    return _out(render.answer(plugin, gid, question=question, res=result))


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
        return _out(
            f"❌ 补采失败：{e}\n\n"
            "已入库的消息和待办不受影响；实时采集照常工作，只是翻不到更早的记录。\n"
            "如果一直这样，多半是这个 NapCat 版本的 `get_group_msg_history` 用不了 —— "
            "把 `config.json` 里 `history.max_pages` 设成 0 可以跳过补采。")
    total = store.message_count(gid)
    return _out(f"✅ 补采完成：新入库 {saved} 条（窗口 {render.span_text(start_ts, end_ts)}）。\n"
                f"本群累计 {total} 条。现在可以 `/sum summary` 了。")


# ------------------------------------------------------------------ 动作：状态

async def _act_status(plugin, event, rest):
    return _out(render.status(plugin))


# ------------------------------------------------------------------ 动作：配置

async def _act_config(plugin, event, rest):
    # 全局配置先摘出来：`/sum config key sk-1234567890` 里那串数字会被
    # 当成群号，走不到正确分支。
    head = CONFIG_ALIASES.get(str(rest[0]).lower()) if rest else None
    if head in GLOBAL_ITEMS:
        return _out(await _CONFIG_ACTIONS[head](plugin, event, None, rest[1:]))

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
                    "push / model / mode / switch / default / key / api / test。")
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
    # 顺手把群名拉下来 —— 之后列群、按名字选群都靠它
    name = await _fetch_group_name(plugin, gid)
    if name:
        plugin.update_group(gid, name=name)
    head = "ℹ️ 本来就在托管" if existed else "✅ 已托管"
    lines = [f"{head}：{render.group_title(plugin, gid)}"]
    if not name:
        lines.append("（没拿到群名，可以手动设：`/sum config name 计院通知群`）")
    lines.append(f"窗口 {plugin.window_hours(gid)}h · 每日推送 {plugin.push_time(gid) or '关'}")
    if not plugin.persona_for(gid):
        lines.append("")
        lines.append("想让总结更贴你？设个身份：`/sum config persona 26级学委`")
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


# ------------------------------------------------------------------ 配置：模型

async def _cfg_key(plugin, event, gid, args):
    """配置模型密钥（全局）。

    只私聊能用（命令上已声明 `scope="private"`），且限超管 —— 这一条改动
    影响所有群和账单，不该跟群的开关一个待遇。发完**立刻撤回**那条消息，
    免得密钥以明文留在聊天记录里。
    """
    if not _is_superuser(plugin, event):
        return "模型密钥只有超管能改。要给别人改，先 `/userperm set <QQ> superuser`。"

    current = plugin.llm_cfg()["api_key"]
    if not args:
        if not current:
            return ("还没配密钥。发：`/sum config key sk-xxxx`\n"
                    "想一条写完：`/sum config key sk-xxxx https://api.deepseek.com/v1`")
        return (f"当前密钥：{render.mask_key(current)}\n"
                "换一个：`/sum config key sk-xxxx`　清空：`/sum config key clear`")

    head = str(args[0]).strip().strip("\"'`")
    if head.lower() in ("clear", "off", "none", "清空", "删除"):
        plugin.update_llm(api_key="")
        return "✅ 已清空密钥（总结会失败，补采和清单还能用）。"

    # 顺手支持一条命令配两样：第一个词是 key，第二个词像地址就当 base_url
    api = None
    if len(args) >= 2:
        api = _clean_base_url(args[1])
        if api is None:
            return ("第二段不像接口地址。要么只给密钥，要么写成 "
                    "`/sum config key sk-xxxx https://api.deepseek.com/v1`。")

    key = _clean_key(head)
    if not key:
        return ("这不像密钥。常见形态是 `sk-` 开头的一长串；"
                "引号、`Bearer ` 前缀、前后空格会自动去掉。")

    plugin.update_llm(api_key=key, base_url=api)
    recalled = await _recall(plugin, event)
    cfg = plugin.llm_cfg()
    lines = [f"✅ 已保存密钥：{render.mask_key(key)}"]
    if api:
        lines.append(f"   接口同时设为 {api}")
    lines.append(f"   模型 {cfg['model']}")
    lines.append("")
    lines.append("（含密钥的这条消息已撤回）" if recalled
                 else "（没能自动撤回——机器人一般删不掉你的私聊消息，"
                      "建议手动删掉这条带密钥的记录）")
    lines.append("立即生效，不用 reload。验一下：`/sum config test`")
    return "\n".join(lines)


async def _cfg_api(plugin, event, gid, args):
    """配置 OpenAI 兼容接口地址（全局）。"""
    if not _is_superuser(plugin, event):
        return "接口地址只有超管能改。"

    current = plugin.llm_cfg()["base_url"]
    if not args:
        return (f"当前接口：{current}\n"
                "改法：`/sum config api https://api.deepseek.com/v1`（clear = 恢复默认）\n"
                "常见写法：\n"
                "  DeepSeek   https://api.deepseek.com/v1\n"
                "  通义千问   https://dashscope.aliyuncs.com/compatible-mode/v1\n"
                "  本地 vLLM  http://127.0.0.1:8000/v1")

    raw = str(args[0]).strip().strip("\"'`")
    if raw.lower() in ("clear", "reset", "default", "默认", "恢复"):
        plugin.update_llm(base_url=DEFAULT_BASE_URL)
        return f"✅ 已恢复默认接口：{DEFAULT_BASE_URL}"

    url = _clean_base_url(raw)
    if url is None:
        return ("这不像接口地址。要以 http:// 或 https:// 开头，并且是 OpenAI 兼容的 "
                "`/v1` 路径，例如 `https://api.deepseek.com/v1`。")
    plugin.update_llm(base_url=url)
    return f"✅ 已保存接口：{url}\n立即生效。验一下：`/sum config test`"


async def _cfg_test(plugin, event, gid, args):
    """真发一次请求，确认 key / 接口 / 模型名能配合上。

    比「保存成功」有用得多 —— 配错接口最常见的结果就是存住了但一次都调不通。
    """
    cfg = plugin.llm_cfg()
    if not cfg["api_key"]:
        return "还没配密钥：`/sum config key sk-xxxx`"
    started = time.time()
    try:
        raw = await llm.chat(plugin, [{"role": "user", "content": "只回复两个字母：ok"}],
                             model=cfg["model"])
    except Exception as e:                     # noqa: BLE001 - 任何失败都要如实说
        reason = getattr(e, "user_message", None) or str(e) or type(e).__name__
        return (f"❌ 调不通\n接口：{cfg['base_url']}\n模型：{cfg['model']}\n原因：{reason}")
    cost = int((time.time() - started) * 1000)
    answer = (raw or "").strip().replace("\n", " ")[:60] or "（空回复）"
    return (f"✅ 调通了（{cost} ms）\n接口：{cfg['base_url']}\n"
            f"模型：{cfg['model']}\n回复：{answer}")


async def _fetch_group_name(plugin, gid) -> str:
    """问接口要群名。拿不到就算了 —— 名字只影响好不好选，不影响功能。"""
    api = getattr(plugin, "api", None)
    if api is None:
        return ""
    try:
        info = await api.get_group_info(str(gid))
    except Exception as e:                 # noqa: BLE001
        plugin.logger.info("[qqnotice_sum] 取群名失败（%s）：%s", gid, e)
        return ""
    if isinstance(info, dict):
        name = info.get("group_name") or info.get("name")
    else:
        name = getattr(info, "group_name", None) or getattr(info, "name", None)
    return str(name or "").strip()[:60]


async def _cfg_name(plugin, event, gid, args):
    """给群起名（接口拿不到、或者想叫顺口一点）。名称同时是选择群的关键词。"""
    rest = list(args)
    if gid is None and len(rest) >= 2:
        matched = _match_group_name(plugin, rest[0])
        if matched is not None:
            gid = matched
            rest = rest[1:]
    gid = gid or _first_group_id(rest)
    if gid is None:
        return _need_group(plugin, "/sum config name <群号或群名> <新名称>")
    rest = [t for t in rest if schemas.parse_group_id(t) is None]
    if not rest:
        current = str(plugin.group_cfg(gid).get("name") or "").strip()
        hint = "" if current else "\n（没有名字，试 `/sum config name 计院通知群`；也可以 `name auto` 从接口拉）"
        return f"当前名称：{current or '（未设置）'}{hint}"
    name = " ".join(rest).strip()[:60]
    if name.lower() == "auto":
        fetched = await _fetch_group_name(plugin, gid)
        if not fetched:
            return "接口没给回群名（可能是权限或连接问题），手动起一个吧。"
        name = fetched
    plugin.update_group(gid, name=name)
    return f"✅ 名称改成「{name}」\n以后可以直接说：`/sum summary {name}`"


async def _cfg_persona(plugin, event, gid, args):
    """设置总结时的「身份」——它决定哪些事算你的、哪些可以不管。"""
    rest = list(args)
    if gid is None and len(rest) >= 2:
        matched = _match_group_name(plugin, rest[0])
        if matched is not None:
            gid = matched
            rest = rest[1:]
    gid = gid or _first_group_id(rest)
    rest = [t for t in rest if schemas.parse_group_id(t) is None]

    if not rest:
        return _persona_overview(plugin, gid)

    text = " ".join(rest).strip()
    target = gid
    if text.lower() in ("clear", "off", "none", "清空", "取消"):
        if target is None:
            plugin.set_default("persona", "")
            return "✅ 已清空全局身份。各群自己设过的身份不受影响。"
        plugin.update_group(target, persona="")
        return f"✅ 已清空「{render.group_title(plugin, target)}」的身份。"

    if target is None:
        plugin.set_default("persona", text[:600])
        return (f"✅ 已设为全局身份：{text[:600]}\n"
                "（所有没单独设过的群都按这个来。群级的会覆盖它。）")
    plugin.update_group(target, persona=text[:600])
    say = "\n以后这个群的总结和多群时就直接说：「/sum summary " +           (str(plugin.group_cfg(target).get("name") or "") or str(target)) + "」"
    return f"✅ 「{render.group_title(plugin, target)}」的身份：{text[:600]}{say}"


def _persona_overview(plugin, gid=None) -> str:
    """`/sum config persona` 的输出：多群时逐群列，别只报全局。"""
    global_persona = plugin.defaults()["persona"]
    lines: list[str] = []
    if gid is None:
        gids = sorted(plugin.group_ids())
        lines.append("全局身份：" + (global_persona or "未设置"))
        if gids:
            lines.append("")
            lines.append("各群：")
            for one in gids[:10]:
                local = str(plugin.group_cfg(one).get("persona") or "").strip()
                if local:
                    shown = local
                elif global_persona:
                    shown = f"{global_persona}（继承全局）"
                else:
                    shown = "未设置"
                lines.append(f"  {render.group_title(plugin, one)} → {shown}")
            if len(gids) > 10:
                lines.append(f"  …还有 {len(gids) - 10} 个")
    else:
        local = str(plugin.group_cfg(gid).get("persona") or "").strip()
        if local:
            lines.append(f"{render.group_title(plugin, gid)} 的身份：{local}")
        elif global_persona:
            lines.append(f"{render.group_title(plugin, gid)} 的身份：（继承全局）{global_persona}")
        else:
            lines.append(f"{render.group_title(plugin, gid)} 的身份：未设置")

    lines.append("")
    lines.append("设了身份之后，总结会按那个视角筛：只留你真正要跟进的，"
                 "无关的（别的年级、别的部门、纯通知）不往待办里塞。")
    lines.append("")
    lines.append("写法：`/sum config persona 26级学委`")
    lines.append("      `/sum config persona 班级心理委员，只关心统计和上报`")
    lines.append("      `/sum config persona clear` 清空")
    lines.append("多群时前面加群号或群名，只对那个群生效。")
    return "\n".join(lines)


async def _cfg_incremental(plugin, event, gid, args):
    """增量总结开关（全局：影响所有群的手动总结与定时推送）。"""
    if not args:
        on = plugin.incremental()
        return (f"增量总结：{'开' if on else '关'}\n"
                "  开 —— 每次从上一次总结结束的时刻开始，只处理新消息（省上下文）\n"
                "  关 —— 每次回溯固定窗口（window_hours），会重复总结同段内容\n"
                f"  上限 —— 一次最多往回补 {plugin.max_incremental_hours()} 小时\n"
                "改：`/sum config incremental on|off`")
    if not _is_superuser(plugin, event):
        return "这个开关影响所有群，只有超管能改。"
    value = schemas.parse_bool(args[0])
    if value is None:
        return "用法：`/sum config incremental on|off`"
    plugin.set_default("incremental", value)
    tail = ("以后每次只处理上次之后的新消息。" if value
            else "以后每次都回溯 window_hours，注意会重复处理已经总结过的内容。")
    return f"✅ 增量总结已{'开启' if value else '关闭'}。{tail}"


def _is_superuser(plugin, event) -> bool:
    """框架的超管判定；拿不到权限管理器（冒烟/离线）时不拦。"""
    perm = getattr(plugin, "perm", None)
    if perm is None:
        return True
    try:
        return bool(perm.is_superuser(int(getattr(event, "user_id", 0) or 0)))
    except (TypeError, ValueError):
        return False


async def _recall(plugin, event) -> bool:
    """撤回那条带密钥的命令消息；拿不到 api / 没权限就静默失败。"""
    api = getattr(plugin, "api", None)
    message_id = getattr(event, "message_id", None)
    if api is None or not message_id:
        return False
    try:
        await api.delete_msg(int(message_id))
        return True
    except Exception:                          # noqa: BLE001 - 撤不掉不影响保存
        return False


def _clean_key(text: str) -> str:
    """清掉粘贴时带上的引号、反引号、`Bearer ` 前缀和零宽字符。"""
    key = str(text or "").strip().strip("\"'`，,。")
    if key.lower().startswith("bearer "):
        key = key[7:].strip()
    key = key.replace(" ", "").replace("\u200b", "").replace("\u3000", "")
    if len(key) < 8:
        return ""
    # 真密钥都是可打印 ASCII。挡掉「我的key是xxx」这类误发 —— 否则它会被
    # 当成密钥存下来，之后每次总结都拿它去撞接口，报的错还看不懂。
    if any(ord(ch) < 33 or ord(ch) > 126 for ch in key):
        return ""
    return key[:200]


def _clean_base_url(text: str) -> Optional[str]:
    """接口地址规范化：补 scheme、去尾斜杠、挡掉明显不是 URL 的输入。

    允许 `api.deepseek.com/v1` 这种省了 scheme 的写法 —— 粘贴时很常见。
    """
    raw = str(text or "").strip().strip("\"'`")
    if not raw:
        return None
    if "://" not in raw:
        host = raw.split("/")[0]
        if "." not in host and not host.startswith(("localhost", "127.0.0.1")):
            return None
        raw = "https://" + raw

    try:
        parts = urlsplit(raw)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    host = parts.netloc.split(":")[0]
    if "." not in host and not host.startswith(("localhost", "127.0.0.1")):
        return None
    try:
        _port = parts.port                         # 读一次触发端口合法性校验
    except ValueError:
        return None
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))


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
    "ask": _act_ask,
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
    "key": _cfg_key,
    "api": _cfg_api,
    "test": _cfg_test,
    "incremental": _cfg_incremental,
    "name": _cfg_name,
    "persona": _cfg_persona,
}
