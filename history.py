# -*- coding: utf-8 -*-
"""群历史消息补采。

两条路一起用：

* **实时**（`events/message.py`）—— 插件跑着的时候每条消息直接落库；
* **补采**（本模块）—— 冷启动当天、机器人掉线期间、插件刚装上的时候，
  用 `get_group_msg_history` 把缺的补回来。

两个必须知道的接口脾气：

1. 每次调用都有 0.5~1s 节流（框架串行），所以补采只放在**连接就绪回调**和
   **定时任务**里，绝不放在用户命令的同步路径上；
2. `message_id` 不保证递增，翻页游标要用 **`message_seq`**。
"""
from __future__ import annotations

import time
from typing import Optional

from . import collect, store

#: 停机多久算「漏了」，值得补一次（秒）。太小会把正常抖动当成掉线。
CATCH_UP_GAP = 600


def _int(value, default: int = 0) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return default


def _batch_of(data) -> list[dict]:
    """接口返回形态三种都认：`{"messages": [...]}` / `{"data": [...]}` / 裸列表。"""
    if isinstance(data, dict):
        batch = data.get("messages") or data.get("data") or []
    elif isinstance(data, list):
        batch = data
    else:
        batch = []
    return [m for m in batch if isinstance(m, dict) and m.get("message_id")]


async def fetch_page(api, group_id, *, cursor: Optional[int] = None,
                     count: int = 40) -> list[dict]:
    """取一页历史（新 → 旧）。

    **`message_seq` 的坑**：不传时框架会把这个参数整个省略，但有些 NapCat
    版本不认「省略」，会拿 `undefined` 去查消息，直接回
    `[1200] 消息undefined不存在`。所以第一页走两段式：先按省略调，报错了
    再显式给 `message_seq=0`（多数实现里 0 就是「从最新开始」）。

    翻页时游标是确定的，没有这个歧义。
    """
    attempts: list[Optional[int]] = [int(cursor)] if cursor else [None, 0]
    last_error: Optional[BaseException] = None
    for seq in attempts:
        extra = {"message_seq": seq} if seq is not None else {}
        try:
            data = await api.get_group_msg_history(
                str(group_id), count=int(count),
                reverse_order=True, **extra)
        except Exception as e:                # noqa: BLE001 - 换一种传法再试
            last_error = e
            continue
        return _batch_of(data)

    raise RuntimeError(
        f"拉取群 {group_id} 历史消息失败（不传 message_seq 与传 0 都试过）："
        f"{last_error}") from last_error


async def backfill(plugin, group_id, *, since_ts: int, max_pages: Optional[int] = None,
                   page_size: Optional[int] = None) -> int:
    """把 `since_ts` 之后的历史消息补进库。返回新入库条数。

    Args:
        since_ts: 只补这个时间点之后的消息；传 0 表示「能补多少补多少」（受 max_pages 限）。
        max_pages: 最多翻几页（每页 `page_size` 条），默认取配置。
        page_size: 每页条数，默认取配置。
    """
    api = getattr(plugin, "api", None)
    if api is None:
        return 0
    cfg = plugin.history_cfg()
    max_pages = int(cfg["max_pages"] if max_pages is None else max_pages)
    if max_pages <= 0:
        return 0                          # 配置里关掉了历史补采
    page_size = int(page_size or cfg["page_size"])

    rows: list[dict] = []
    cursor: Optional[int] = None
    for _page in range(max_pages):
        batch = await fetch_page(api, group_id, cursor=cursor, count=page_size)
        if not batch:
            break
        hit_boundary = False
        for raw in batch:
            ts = _int(raw.get("time"))
            if since_ts and ts and ts < since_ts:
                hit_boundary = True
                continue                  # 同一页里可能还有边界内的，别整页丢
            row = collect.history_row(raw)
            if row is not None:
                rows.append(row)
        if hit_boundary:
            break
        oldest = min(batch, key=lambda m: _int(m.get("time")))
        next_cursor = _int(oldest.get("message_seq")) or _int(oldest.get("message_id"))
        if not next_cursor or next_cursor == cursor:
            break                         # 游标没动 = 到底了，别死循环
        cursor = next_cursor
        if len(batch) < page_size:
            break

    if not rows:
        _touch(plugin, group_id, rows=[])
        return 0
    db = plugin.database()
    saved = store.save_messages(db, group_id, rows)
    _touch(plugin, group_id, rows=rows)
    plugin.logger.info("[qqnotice_sum] 群 %s 补采：翻 %d 页、拿到 %d 条、新入库 %d 条",
                       group_id, _page + 1, len(rows), saved)
    return saved


def _touch(plugin, group_id, *, rows: list[dict]) -> None:
    """更新采集游标（没有新数据时只刷新 `last_sync`）。"""
    latest_ts = max((_int(r.get("ts")) for r in rows), default=0)
    latest_seq = max((_int(r.get("seq")) for r in rows), default=0)
    try:
        store.touch_sync(group_id, last_ts=latest_ts, last_seq=latest_seq)
    except Exception:                          # noqa: BLE001 - 游标写失败不该影响主流程
        plugin.logger.warning("[qqnotice_sum] 更新采集游标失败（群 %s）", group_id, exc_info=True)


async def backfill_recent(plugin, group_id, *, hours: int) -> int:
    """补最近 N 小时。"""
    since = int(time.time() - max(1, int(hours)) * 3600)
    return await backfill(plugin, group_id, since_ts=since)


async def catch_up(plugin, group_id, *, hours: int = 48,
                   max_pages: Optional[int] = None) -> int:
    """停机补漏：只补「库里最后一条之后」的缺口。

    库里一条都没有时退回 `backfill_recent(hours)`。
    """
    last_ts = 0
    try:
        last_ts = store.latest_message_ts(group_id)
    except Exception:                          # noqa: BLE001
        last_ts = 0
    now = int(time.time())
    if not last_ts or now - last_ts > hours * 3600:
        return await backfill_recent(plugin, group_id, hours=hours)
    if now - last_ts < CATCH_UP_GAP:
        return 0                               # 刚同步过，不用补
    return await backfill(plugin, group_id, since_ts=last_ts - 60,
                          max_pages=max_pages)


__all__ = ["fetch_page", "backfill", "backfill_recent", "catch_up", "CATCH_UP_GAP"]
