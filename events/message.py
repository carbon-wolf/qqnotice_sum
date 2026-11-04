# -*- coding: utf-8 -*-
"""实时采集：托管群里每来一条消息就落库。

这里**不判断有没有 @ 机器人** —— 框架只在「命令分发」那一层要求 @，
事件层是全量分发的。所以只要群在框架的监听范围里、又在插件的托管名单里，
每条消息都会进来。

采集只做三件事：过滤（不是群消息 / 不是托管群 / 自己发的）→ 文本化 → 入库。
清洗留到总结时再做（原始消息要保真，清洗规则以后改了还能重跑）。
"""
from __future__ import annotations

from napcat.models import MessageEvent
from napcat.registry import event

from .. import collect, store


@event(MessageEvent, description="把托管群的消息落库")
async def record(plugin, event):
    if not plugin.is_on():
        return
    try:
        group_id = int(getattr(event, "group_id", 0) or 0)
    except (TypeError, ValueError):
        return
    if group_id <= 0 or not plugin.is_watched(group_id):
        return

    self_id = int(getattr(event, "self_id", 0) or 0)
    row = collect.event_row(event, self_id=self_id)
    if row is None:
        return
    if self_id and row.get("user_id") == self_id:
        return                              # 机器人自己的播报不该进原料
    try:
        saved = store.save_messages(plugin.database(), group_id, [row])
    except Exception:                       # noqa: BLE001 - 一条写失败不该影响后续
        plugin.logger.warning("[qqnotice_sum] 消息入库失败（群 %s / 消息 %s）",
                              group_id, row.get("message_id"), exc_info=True)
        return
    if not saved:
        return                              # 重复消息（历史补采已经存过）

    # 游标先记在内存里，由定时任务批量落库 —— 每条消息一次 UPDATE 太浪费
    prev = plugin._pending_sync.get(group_id, (0, 0))
    plugin._pending_sync[group_id] = (
        max(prev[0], int(row.get("ts") or 0)),
        max(prev[1], int(row.get("seq") or 0)),
    )
