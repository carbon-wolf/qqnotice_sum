# -*- coding: utf-8 -*-
"""总结流程 —— 手动命令与定时推送共用同一条链路。

顺序固定：

    补采缺口 → 取窗口内消息 → 清洗合并裁剪 → 标注附件 → 取图/解析文件
    → 调模型（带图） → 解析 → 待办入库 → 回结构化结果

把这段独立出来，是为了让「私聊 `/sum summary`」和「晚上 9 点半自动推」走
**完全一样**的逻辑（包括去重与指纹），否则两边的待办会各算一套。
"""
from __future__ import annotations

import time

from . import digest, history, llm, media, qa, store

#: 三种产出模式
MODES = ("all", "todo", "digest")


def mode_label(mode: str) -> str:
    return {"todo": "只看待办", "digest": "只看摘要"}.get(mode, "摘要 + 待办")


async def summarize(plugin, group_id: int, *, start_ts: int, end_ts: int,
                    model: str = "", mode: str = "all",
                    catch_up_pages: int = 3) -> dict:
    """跑一次总结。

    Args:
        mode: `all`（摘要 + 待办）/ `todo`（只要待办，跳过图片与附件）/
              `digest`（只要摘要，待办照样入库但不渲染）。

    Returns:
        dict: `ok / empty / structured / raw / digest / summary / points / todos /
               created / open / model / cost_ms / mode / media / media_skipped`
    """
    gid = int(group_id)
    mode = mode if mode in MODES else "all"
    base = {"ok": False, "empty": False, "structured": False, "raw": "",
            "digest": {"raw": 0, "kept": 0, "sampled": False},
            "summary": "", "points": [], "todos": [],
            "created": 0, "open": 0, "model": "",
            "cost_ms": 0, "mode": mode,
            "start_ts": int(start_ts), "end_ts": int(end_ts),
            "media": {"images": 0, "files": 0}, "media_skipped": []}

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
    media_cfg = plugin.media_cfg()
    data = digest.build(rows, clean_cfg=clean_cfg,
                        max_chars=llm_cfg["max_context_chars"])
    if not data["lines"]:
        return {**base, "empty": True, "reason": "all_noise", "digest": data}

    # 3) 标注附件并按编号重排文本（[图片] → [图1]，[文件:x] → [文件1:x]）
    media_out = {"images": [], "files": [], "skipped": [], "notes": []}
    if mode != "todo" and media_cfg.get("enabled", True):
        tagged, items = media.tag_rows(data.get("rows") or [])
        if items:
            data = dict(data)
            data["text"] = "\n".join(digest.format_line(row) for row in tagged)
            try:
                media_out = await media.gather(plugin, gid, items, cfg=media_cfg)
            except Exception as e:         # noqa: BLE001 - 附件失败不该让总结失败
                plugin.logger.warning("[qqnotice_sum] 附件处理失败（群 %s）：%s", gid, e)

    # 4) 调模型
    use_model = (model or plugin.model_for(gid) or llm_cfg["model"]).strip()
    messages = digest.build_chat_messages(
        group_name=_group_name(plugin, gid), digest=data,
        start_ts=start_ts, end_ts=end_ts, max_todos=plugin.max_todos(gid),
        files_section=media.build_file_section(media_out["files"]),
        images=media_out["images"],
        image_note=media.image_note(media_out["images"]))
    started = time.time()
    raw = await llm.chat(plugin, messages, model=use_model)
    cost_ms = int((time.time() - started) * 1000)

    result = {**base, "raw": raw, "digest": data, "model": use_model,
              "cost_ms": cost_ms,
              "media": {"images": len(media_out["images"]),
                        "files": len(media_out["files"])},
              "media_skipped": list(media_out.get("skipped") or [])}

    # 5) 解析；解析不出来就明确告诉用户「这次没结构化」，不假装成功
    report = llm.parse_report(raw)
    if report is None:
        return result
    result["summary"] = report["summary"]
    result["points"] = report["points"]
    result["todos"] = report["todos"]
    result["structured"] = True

    # 6) 待办入库（指纹去重，跨多次总结不会重复计数）
    run = store.record_run(gid, start_ts=start_ts, end_ts=end_ts,
                           msg_count=data["kept"], todo_new=0, todo_open=0,
                           model=use_model, cost_ms=cost_ms, mode=mode,
                           media_count=len(media_out["images"]) + len(media_out["files"]),
                           summary=report["summary"], points=report["points"])
    created, open_total = store.upsert_todos(gid, report["todos"], run_id=run.pk)
    run.todo_new = created
    run.todo_open = open_total
    run.save()

    result.update({"ok": True, "created": created, "open": open_total})
    if created:
        plugin.logger.info("[qqnotice_sum] 群 %s 新增待办 %d 条（未完成共 %d 条）",
                           gid, created, open_total)
    return result


async def ask(plugin, group_id: int, *, question: str, start_ts: int, end_ts: int,
              model: str = "", catch_up_pages: int = 2) -> dict:
    """就群里的内容提一个问题。**只读**：不写待办，也不碰总结的冷却。

    与 `summarize()` 共用补采和清洗，区别在「挑消息」这一步 ——
    总结要时间分布均匀，问答要包含答案的那几条（见 `qa.select`）。

    Returns:
        dict: `ok / empty / reason / structured / answer / refs / cites /
               enough / question / total / picked / trimmed / model / cost_ms`
    """
    gid = int(group_id)
    base = {"ok": False, "empty": False, "structured": False, "raw": "",
            "answer": "", "refs": [], "cites": [], "enough": True,
            "question": question, "total": 0, "picked": 0, "trimmed": False,
            "model": "", "cost_ms": 0,
            "start_ts": int(start_ts), "end_ts": int(end_ts)}

    # 1) 先把缺口补上，否则「刚才那条」问不到
    try:
        await history.catch_up(
            plugin, gid,
            hours=max(1, int((end_ts - start_ts) / 3600) + 1),
            max_pages=catch_up_pages)
    except Exception as e:                 # noqa: BLE001 - 补采失败不影响用已有数据
        plugin.logger.warning("[qqnotice_sum] 提问前补采失败（群 %s）：%s", gid, e)

    rows = store.messages_between(gid, start_ts, end_ts)
    if not rows:
        return {**base, "empty": True, "reason": "no_messages"}

    clean_cfg = plugin.clean_cfg()
    llm_cfg = plugin.llm_cfg()
    # 问答比总结更依赖「那一条」，预算给足；但不吃满上下文，留位置给回答
    selected = qa.select(rows, question, clean_cfg=clean_cfg,
                         max_chars=min(llm_cfg["max_context_chars"], 12000))
    if not selected["items"]:
        return {**base, "empty": True, "reason": "all_noise"}

    use_model = (model or llm_cfg["model"]).strip()
    messages = qa.build_messages(group_name=_group_name(plugin, gid),
                                 question=question, selected=selected,
                                 start_ts=start_ts, end_ts=end_ts)
    started = time.time()
    raw = await llm.chat(plugin, messages, model=use_model)
    cost_ms = int((time.time() - started) * 1000)

    parsed = qa.parse_answer(raw)
    if parsed is None:
        # 没按 JSON 回：把原文当答案显示（问答场景纯文本也读得懂），只是没引用
        return {**base, "ok": True, "structured": False,
                "answer": qa.strip_fence(raw)[:1500], "raw": raw,
                "total": selected["total"], "picked": selected["picked"],
                "trimmed": selected["trimmed"], "model": use_model,
                "cost_ms": cost_ms}

    return {**base, "ok": True, "structured": True, "raw": raw,
            "answer": parsed["answer"], "refs": parsed["refs"],
            "cites": qa.cite_items(selected, parsed["refs"]),
            "enough": parsed["enough"],
            "total": selected["total"], "picked": selected["picked"],
            "trimmed": selected["trimmed"], "model": use_model,
            "cost_ms": cost_ms}


def window_for(plugin, group_id) -> tuple[int, int]:
    """该群默认窗口：`[现在 - N 小时, 现在]`。"""
    end_ts = int(time.time())
    return end_ts - plugin.window_hours(group_id) * 3600, end_ts


def _group_name(plugin, group_id) -> str:
    cfg = plugin.group_cfg(group_id)
    return str(cfg.get("name") or "").strip() or str(group_id)


__all__ = ["summarize", "ask", "window_for", "MODES", "mode_label"]
