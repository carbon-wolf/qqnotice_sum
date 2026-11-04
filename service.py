# -*- coding: utf-8 -*-
"""总结流程 —— 手动命令与定时推送共用同一条链路。

顺序固定：补采缺口 → 取窗口内消息 → 清洗 → 调模型 → 解析 → 入库 → 回结构化结果。
把这段独立出来，是为了让「有人在群里按 `/sum 看`」和「晚上 9 点半自动推」走
**完全一样**的逻辑（包括去重与指纹），否则两边的待办会各算一套。
"""
from __future__ import annotations

import time
from typing import Optional

from . import digest, history, llm, store


async def summarize(plugin, group_id: int, *, start_ts: int, end_ts: int,
                    model: str = "", catch_up_pages: int = 3) -> dict:
    """跑一次总结。

    Returns:
        dict:
            `ok`         是否拿到结构化结果
            `empty`      这段时间没消息（`reason`: no_messages / all_noise）
            `structured` 模型是否按 JSON 输出
            `raw`        模型原文（未结构化时给用户看）
            `digest`     清洗统计（raw / kept / sampled）
            `todos`      待办列表
            `created` / `open` 新增条数 / 当前未完成总数
            `model` / `cost_ms` / `start_ts` / `end_ts`
    """
    gid = int(group_id)
    base = {"ok": False, "empty": False, "structured": False, "raw": "",
            "digest": {"raw": 0, "kept": 0, "sampled": False},
            "todos": [], "created": 0, "open": 0, "model": "",
            "cost_ms": 0, "start_ts": int(start_ts), "end_ts": int(end_ts)}

    # 1) 先把缺口补上（掉线 / 刚装插件 / 上一轮定时失败）
    try:
        await history.catch_up(
            plugin, gid,
            hours=max(1, int((end_ts - start_ts) / 3600) + 1),
            max_pages=catch_up_pages)
    except Exception as e:                 # noqa: BLE001 - 补采失败不影响用已有数据
        plugin.logger.warning("[qqnotice_sum] 总结前补采失败（群 %s）：%s", gid, e)

    # 2) 取窗口内的消息
    rows = store.messages_between(gid, start_ts, end_ts)
    if not rows:
        return {**base, "empty": True, "reason": "no_messages"}

    clean_cfg = plugin.clean_cfg()
    llm_cfg = plugin.llm_cfg()
    data = digest.build(rows, clean_cfg=clean_cfg,
                        max_chars=llm_cfg["max_context_chars"])
    if not data["lines"]:
        return {**base, "empty": True, "reason": "all_noise", "digest": data}

    # 3) 调模型
    use_model = (model or plugin.model_for(gid) or llm_cfg["model"]).strip()
    messages = digest.build_chat_messages(
        group_name=_group_name(plugin, gid), digest=data,
        start_ts=start_ts, end_ts=end_ts, max_todos=plugin.max_todos(gid))
    started = time.time()
    raw = await llm.chat(plugin, messages, model=use_model)
    cost_ms = int((time.time() - started) * 1000)

    result = {**base, "raw": raw, "digest": data, "model": use_model,
              "cost_ms": cost_ms}

    # 4) 解析；解析不出来就明确告诉用户「这次没结构化」，不假装成功
    todos = llm.parse_todos(raw)
    if todos is None:
        return result
    result["todos"] = todos
    result["structured"] = True

    # 5) 入库（指纹去重，跨多次总结不会重复计数）
    run = store.record_run(gid, start_ts=start_ts, end_ts=end_ts,
                           msg_count=data["kept"], todo_new=0, todo_open=0,
                           model=use_model, cost_ms=cost_ms)
    created, open_total = store.upsert_todos(gid, todos, run_id=run.pk)
    run.todo_new = created
    run.todo_open = open_total
    run.save()

    result.update({"ok": True, "created": created, "open": open_total})
    if created:
        plugin.logger.info("[qqnotice_sum] 群 %s 新增待办 %d 条（未完成共 %d 条）",
                           gid, created, open_total)
    return result


def window_for(plugin, group_id) -> tuple[int, int]:
    """该群默认窗口：`[现在 - N 小时, 现在]`。"""
    end_ts = int(time.time())
    return end_ts - plugin.window_hours(group_id) * 3600, end_ts


def _group_name(plugin, group_id) -> str:
    cfg = plugin.group_cfg(group_id)
    return str(cfg.get("name") or "").strip() or str(group_id)


__all__ = ["summarize", "window_for"]
