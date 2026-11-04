# -*- coding: utf-8 -*-
"""定时任务：到点推送 / 停机补漏 / 历史清理。

用 `@every(60)` 固定 tick 而不是 `@daily("21:30")`，原因很实际：推送时段是
**配置项**，`@daily` 在注册时就固定了时刻，改配置得重挂定时器（还得记住重挂）。
60 秒醒一次、到点才干活，改配置立刻生效。

三件事都做了防重入/节流：

* 推送用 `last_push_date` 防一天推两次；
* 补漏按群节流（`CATCHUP_THROTTLE`），静默群不会被反复扫；
* 清理按天做，用进程内标记。
"""
from __future__ import annotations

import time

from napcat.message import MessageChain
from napcat.registry import every

from .. import history, render, service, store

#: 同一个群两次「停机补漏」的最小间隔（秒）
CATCHUP_THROTTLE = 1800

#: 补漏时最多翻几页（多数情况一页就够，别把接口节流用满）
CATCHUP_PAGES = 2

#: 清理检查间隔（秒）——按天做事，每小时看一眼就够
CLEANUP_EVERY = 3600

TICK_SECONDS = 60


@every(TICK_SECONDS, name="qqnotice_sum.tick",
       description="到点总结推送 / 停机补漏 / 清理历史")
async def tick(plugin):
    if not plugin.is_on():
        return
    now = int(time.time())
    _flush_sync(plugin)
    _maybe_cleanup(plugin, now)

    for group_id in sorted(plugin.group_ids()):
        if not plugin.is_watched(group_id):
            continue
        try:
            if plugin.push_on(group_id) and _due_for_push(plugin, group_id, now):
                await push_group(plugin, group_id, now=now)
            else:
                await _maybe_catchup(plugin, group_id, now)
        except Exception:                   # noqa: BLE001 - 一个群出错不该拖垮其它群
            plugin.logger.warning("[qqnotice_sum] 定时任务处理群 %s 失败",
                                  group_id, exc_info=True)


# ------------------------------------------------------------------ 推送

def _today(now: int) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(now))


def _due_for_push(plugin, group_id, now: int) -> bool:
    push_time = plugin.push_time(group_id)
    if not push_time:
        return False
    if time.strftime("%H:%M", time.localtime(now)) != push_time:
        return False
    cfg = plugin.group_cfg(group_id)
    return str(cfg.get("last_push_date") or "") != _today(now)


async def push_group(plugin, group_id, *, now: int = 0) -> bool:
    """对某个群跑一次总结并推到群里。返回是否真的发出去了。"""
    now = int(now or time.time())
    if group_id in plugin._pushing:
        return False
    plugin._pushing.add(group_id)
    try:
        start_ts, end_ts = service.window_for(plugin, group_id)
        # 先打标，避免这一分钟里被重复触发；失败也不再补推（下一分钟条件就不成立了）
        plugin.update_group(group_id, last_push_date=_today(now))
        result = await service.summarize(plugin, group_id,
                                         start_ts=start_ts, end_ts=end_ts,
                                         mode=plugin.mode_for(group_id),
                                         catch_up_pages=CATCHUP_PAGES)
        if result.get("empty"):
            plugin.logger.info("[qqnotice_sum] 群 %s 定时总结没有可总结的消息，跳过推送",
                               group_id)
            return False
        text = render.result(plugin, group_id, result)
        await send_text(plugin, group_id, "⏰ 每日待办\n\n" + text)
        return True
    except Exception:                       # noqa: BLE001 - 定时任务不能把整个 tick 打挂
        plugin.logger.warning("[qqnotice_sum] 群 %s 定时推送失败", group_id, exc_info=True)
        return False
    finally:
        plugin._pushing.discard(group_id)


async def send_text(plugin, group_id, text: str) -> None:
    """发一段（可能很长的）文本到群，自动分条。"""
    api = getattr(plugin, "api", None)
    if api is None:
        plugin.logger.warning("[qqnotice_sum] 没有 api，跳过发送（群 %s）", group_id)
        return
    for chain in MessageChain().text(text or "").split(max_chars=1200):
        await api.send_group_msg(group_id=str(group_id), message=chain)


# ------------------------------------------------------------------ 补漏

async def _maybe_catchup(plugin, group_id, now: int) -> None:
    last = plugin._last_catchup.get(group_id, 0)
    if now - last < CATCHUP_THROTTLE:
        return
    plugin._last_catchup[group_id] = now
    try:
        saved = await history.catch_up(plugin, group_id, max_pages=CATCHUP_PAGES)
    except Exception as e:                  # noqa: BLE001 - 掉线期间接口本来就可能失败
        plugin.logger.info("[qqnotice_sum] 群 %s 补漏失败：%s", group_id, e)
        plugin._last_catchup[group_id] = 0  # 失败不占节流额度，下个 tick 还能试
        return
    if saved:
        plugin.logger.info("[qqnotice_sum] 群 %s 补漏入库 %d 条", group_id, saved)


# ------------------------------------------------------------------ 冷启动

async def bootstrap(plugin) -> None:
    """WS 就绪后补一次历史：插件刚装当天不至于只有零星几条消息。"""
    if not plugin.is_on():
        return
    cfg = plugin.history_cfg()
    hours = cfg["bootstrap_hours"]
    for group_id in sorted(plugin.group_ids()):
        if group_id in plugin._bootstrapped or not plugin.is_watched(group_id):
            continue
        plugin._bootstrapped.add(group_id)
        try:
            saved = await history.backfill_recent(plugin, group_id, hours=hours)
            plugin.logger.info("[qqnotice_sum] 群 %s 冷启动补采 %d 条（近 %d 小时）",
                               group_id, saved, hours)
        except Exception as e:              # noqa: BLE001
            plugin.logger.info("[qqnotice_sum] 群 %s 冷启动补采失败：%s", group_id, e)
            plugin._bootstrapped.discard(group_id)   # 下次重连再试


# ------------------------------------------------------------------ 收尾

def _flush_sync(plugin) -> None:
    """把实时采集攒下的游标写库（每条消息都写一次太浪费）。"""
    if not plugin._pending_sync:
        return
    for group_id, (ts, seq) in list(plugin._pending_sync.items()):
        try:
            store.touch_sync(group_id, last_ts=ts, last_seq=seq)
        except Exception:                   # noqa: BLE001
            plugin.logger.warning("[qqnotice_sum] 落库采集游标失败（群 %s）", group_id,
                                  exc_info=True)
        finally:
            plugin._pending_sync.pop(group_id, None)


def _maybe_cleanup(plugin, now: int) -> None:
    day = _today(now)
    if plugin._last_cleanup == day:
        return
    plugin._last_cleanup = day
    days = int(plugin.clean_cfg().get("retention_days") or 60)
    if days <= 0:
        return
    cutoff = now - days * 86400
    try:
        removed = store.purge_messages(plugin.database(), before_ts=cutoff)
        if removed:
            plugin.logger.info("[qqnotice_sum] 清理 %d 天前的消息 %d 条", days, removed)
    except Exception:                       # noqa: BLE001 - 清理失败不影响主流程
        plugin.logger.warning("[qqnotice_sum] 清理历史消息失败", exc_info=True)
