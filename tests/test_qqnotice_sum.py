# -*- coding: utf-8 -*-
"""qqnotice_sum 自测：不连 QQ、不连网。

跑法（在框架根目录）：

    ./venv/Scripts/python.exe -B plugins/qqnotice_sum/tests/test_qqnotice_sum.py

覆盖两类：

* **纯函数**：窗口解析、动作解析、清洗合并、指纹、JSON 兜底解析；
* **端到端**：真建插件实例 + 框架 sqlite，喂假群消息 → 私聊发 `/sum` →
  校验入库、去重、群号推断、完成动作。模型调用用假函数替换，不发网络请求。
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
import tempfile
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "plugins"))

from napcat.client import GroupBinding                      # noqa: E402
from napcat.command import CommandManager                   # noqa: E402
from napcat.config import ConfigManager                     # noqa: E402
from napcat.errors import ErrorReporter                     # noqa: E402
from napcat.events import EventBus, TimerManager            # noqa: E402
from napcat.file_manager import FileManager                 # noqa: E402
from napcat.models import MessageEvent, Sender              # noqa: E402
from napcat.permission import PermissionManager             # noqa: E402

from qqnotice_sum import (collect, digest, history, llm, media, qa, render,  # noqa: E402
                          schemas, store)
from qqnotice_sum.commands import main as cmd_main                     # noqa: E402
from qqnotice_sum.events import message as msg_event                   # noqa: E402
from qqnotice_sum.plugin import QQNoticeSumPlugin                       # noqa: E402
from qqnotice_sum.schemas import SumSchema                             # noqa: E402

GROUP = 987654321
GROUP2 = 987654322
SUPER = 999
MEMBER = 123456

_FAILED = []
_PASSED = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        _PASSED.append(name)
        print(f"  ✓ {name}")
    else:
        _FAILED.append(name)
        print(f"  ✗ {name}{(' — ' + detail) if detail else ''}")


# ------------------------------------------------------------------ 纯函数

def test_spec() -> None:
    print("\n[动作解析]")
    check("空 → 面板", schemas.parse_spec([]) == ("panel", []))
    check("summary 24h", schemas.parse_spec(["summary", "24h"]) == ("summary", ["24h"]))
    check("list all", schemas.parse_spec(["list", "all"]) == ("list", ["all"]))
    check("done 12", schemas.parse_spec(["done", "12"]) == ("done", ["12"]))
    check("config add", schemas.parse_spec(["config", "add", "123"]) == ("config", ["add", "123"]))
    check("配置（中文别名）", schemas.parse_spec(["配置", "加群"])[0] == "config")
    check("总结（中文别名）", schemas.parse_spec(["总结"])[0] == "summary")
    check("不认识的动作", schemas.parse_spec(["阿巴阿巴"])[0] == "unknown")


def test_window() -> None:
    print("\n[窗口解析]")
    now = int(time.time())
    span = schemas.parse_window("24h", now=now)
    check("24h = 一天", span is not None and abs((span[1] - span[0]) - 86400) <= 2)
    span = schemas.parse_window("1d12h", now=now)
    check("1d12h = 36 小时", span is not None and abs((span[1] - span[0]) - 129600) <= 2)
    span = schemas.parse_window("90m", now=now)
    check("90m = 5400 秒", span is not None and abs((span[1] - span[0]) - 5400) <= 2)
    span = schemas.parse_window("today", now=now)
    check("today 从 0 点起", span is not None and span[0] < now <= span[1])
    span = schemas.parse_window("yesterday", now=now)
    check("yesterday 是完整一天", span is not None and 86390 <= (span[1] - span[0]) <= 86410)
    span = schemas.parse_window("all", now=now)
    check("all 从 0 开始", span is not None and span[0] == 0)
    check("乱写返回 None", schemas.parse_window("随便") is None)
    check("window_hours(3d) = 72", schemas.window_hours("3d") == 72)


def test_misc_parsers() -> None:
    print("\n[其他解析]")
    check("21:30", schemas.parse_hhmm("21:30") == "21:30")
    check("9:5 → 09:05", schemas.parse_hhmm("9:5") == "09:05")
    check("2130 → 21:30", schemas.parse_hhmm("2130") == "21:30")
    check("关 → 空串", schemas.parse_hhmm("关") == "")
    check("25:00 非法", schemas.parse_hhmm("25:00") is None)
    check("on", schemas.parse_bool("on") is True)
    check("off", schemas.parse_bool("off") is False)
    check("群号 987654321", schemas.parse_group_id("987654321") == 987654321)
    check("群号 123 太短", schemas.parse_group_id("123") is None)


def test_collect() -> None:
    print("\n[消息段文本化]")
    text, kind = collect.segments_to_text([
        {"type": "text", "data": {"text": "明天交报告"}},
        {"type": "at", "data": {"qq": "111", "name": "李四"}},
    ])
    check("纯文本 kind=text", kind == "text" and "明天交报告" in text and "@李四" in text)
    text, kind = collect.segments_to_text([
        {"type": "image", "data": {"file": "a.jpg"}},
        {"type": "text", "data": {"text": "看图"}},
    ])
    check("图片占位 + rich", kind == "rich" and "[图片]" in text)
    text, _ = collect.segments_to_text([
        {"type": "at", "data": {"qq": "999"}}, {"type": "text", "data": {"text": " 好"}}
    ], self_id=999)
    check("@机器人 渲染成 @我", "@我" in text)
    card = json.dumps({"prompt": "[通知] 周五停电", "meta": {"news": {"desc": "全天"}}})
    text, _ = collect.segments_to_text([{"type": "json", "data": {"data": card}}])
    check("JSON 卡片捞出人话", "停电" in text and "全天" in text)
    text, _ = collect.segments_to_text([{"type": "file", "data": {"name": "名单.xlsx"}}])
    check("文件名保留", "名单.xlsx" in text)
    check("空消息 → 空串", collect.segments_to_text([])[0] == "")


def test_digest() -> None:
    print("\n[清洗与合并]")
    cfg = {"noise": ["收到", "好的", "1"], "min_len": 2, "merge_seconds": 300,
           "max_msg_chars": 500}
    base = int(time.time())
    rows = [
        {"sender": "张三", "text": "收到", "ts": base},
        {"sender": "张三", "text": "明天交报告", "ts": base + 10},
        {"sender": "张三", "text": "交到教务系统", "ts": base + 20},
        {"sender": "李四", "text": "👌", "ts": base + 30},
        {"sender": "李四", "text": "[表情]", "ts": base + 40},
        {"sender": "王五", "text": "1", "ts": base + 50},
        {"sender": "赵六", "text": "周五下午三点开会", "ts": base + 400},
    ]
    out = digest.build(rows, clean_cfg=cfg, max_chars=5000)
    text = out["text"]
    check("噪声被滤掉", "收到" not in text and "\n1" not in text)
    check("纯表情被滤掉", "[表情]" not in text)
    check("同一人连发被合并", "明天交报告 / 交到教务系统" in text)
    check("另一人的消息另起一行", "周五下午三点开会" in text)
    check("统计口径可用", out["raw"] == 7 and out["kept"] == 2)

    long_rows = [{"sender": f"人{i}", "text": f"第{i}条消息内容" + "。" * 20,
                  "ts": base + i * 60} for i in range(200)]
    out = digest.build(long_rows, clean_cfg=cfg, max_chars=2000)
    check("超预算会抽稀", out["sampled"] is True and len(out["text"]) <= 2400,
          f"len={len(out['text'])}")

    check("指纹稳定", store.fingerprint(1, "提交报告", "张三") ==
          store.fingerprint(1, "提交 报告。", "张三"))
    check("指纹区分责任人", store.fingerprint(1, "提交报告", "张三") !=
          store.fingerprint(1, "提交报告", "李四"))


def test_llm_parse() -> None:
    print("\n[模型输出解析]")
    payload = {"todos": [{"title": "交报告", "owner": "张三", "due": "2026-11-05 18:00",
                          "priority": "high", "evidence": "明天交"}]}
    got = llm.parse_todos(json.dumps(payload, ensure_ascii=False))
    check("干净 JSON", got is not None and got[0]["title"] == "交报告")
    fenced = "好的，结果如下：\n```json\n" + json.dumps(payload, ensure_ascii=False) + "\n```\n以上。"
    check("带代码围栏 + 废话", (llm.parse_todos(fenced) or [{}])[0].get("title") == "交报告")
    check("裸数组", len(llm.parse_todos('[{"title":"领书"}]') or []) == 1)
    check("空待办", llm.parse_todos('{"todos":[]}') == [])
    check("完全不是 JSON → None", llm.parse_todos("我觉得没什么事") is None)
    check("中文优先级归一", (llm.parse_todos('[{"title":"x","priority":"高"}]') or [{}])[0]
          .get("priority") == "high")


def test_media() -> None:
    print("\n[附件]")
    check("human_size 1.5MB", media.human_size(1572864) == "1.5MB", media.human_size(1572864))
    check("human_size 800B", media.human_size(800) == "800B")

    text, status = media.extract_text("通知.txt", "明天下午三点开会".encode("utf-8"),
                                      max_chars=1000)
    check("txt 直接解码", status == "ok" and "开会" in text)
    text, status = media.extract_text("名单.csv", "姓名,学号\n张三,001".encode("gb18030"),
                                      max_chars=1000)
    check("gb18030 也能读", status == "ok" and "张三" in text)
    text, status = media.extract_text("a.zip", b"PK\x03\x04", max_chars=1000)
    check("不支持的格式标记 unsupported", status == "unsupported")
    text, status = media.extract_text("长.txt", ("啊" * 500).encode(), max_chars=50)
    check("按 max_chars 截断", len(text) <= 60, str(len(text)))

    rows = [
        {"message_id": 1, "text": "[图片][图片]", "media": [
            {"kind": "image", "file": "a.jpg"}, {"kind": "image", "file": "b.jpg"}]},
        {"message_id": 2, "text": "[文件:课表.xlsx]", "media": [
            {"kind": "file", "name": "课表.xlsx", "file_id": "f1", "size": 1024}]},
        {"message_id": 3, "text": "纯文字", "media": []},
    ]
    tagged, items = media.tag_rows(rows)
    check("两张图分别编号", "[图1][图2]" in tagged[0]["text"], tagged[0]["text"])
    check("文件带编号", "[文件1:课表.xlsx]" in tagged[1]["text"], tagged[1]["text"])
    check("无附件行不动", tagged[2]["text"] == "纯文字")
    check("附件清单 3 项", len(items) == 3)
    check("index 记录位置", items[1]["index"] == 1)


async def test_media_limits() -> None:
    """文件大小 / 扩展名 / 数量上限都必须在**下载前**就挡住。"""
    print("\n[附件上限]")
    cfg = {"enabled": True, "timeout": 3, "cache_hours": 72,
           "image": {"enabled": True, "max_bytes": 1024 * 1024, "max_per_run": 2},
           "file": {"enabled": True, "max_bytes": 1024 * 1024, "max_per_run": 2,
                    "max_chars": 100, "allow_ext": [".txt"]}}

    class _P:
        api = None

    def item(name, size, label="文件1"):
        return {"kind": "file", "label": label, "index": 0,
                "descriptor": {"name": name, "size": size, "file_id": "f"},
                "row": {"message_id": 1}}

    out = await media.gather(_P(), 1, [item("大文件.txt", 10 * 1024 * 1024)], cfg=cfg)
    check("超过文件上限直接跳过", not out["files"]
          and any("超过文件上限" in msg for msg in out["skipped"]), str(out["skipped"]))

    out = await media.gather(_P(), 1, [item("脚本.exe", 1024)], cfg=cfg)
    check("扩展名不在白名单就跳过", not out["files"]
          and any("不在允许列表" in msg for msg in out["skipped"]), str(out["skipped"]))

    cfg_off = {**cfg, "file": {**cfg["file"], "enabled": False}}
    out = await media.gather(_P(), 1, [item("笔记.txt", 100)], cfg=cfg_off)
    check("关掉文件处理后什么都不做", not out["files"] and not out["skipped"])

    cfg_img = {**cfg, "image": {**cfg["image"], "max_bytes": 1024}}
    big_image = {"kind": "image", "label": "图1", "index": 0,
                 "descriptor": {"file": "a.jpg", "size": 5 * 1024 * 1024},
                 "row": {"message_id": 1}}
    out = await media.gather(_P(), 1, [big_image], cfg=cfg_img)
    check("超过图片上限就跳过", not out["images"]
          and any("超过单张上限" in msg for msg in out["skipped"]), str(out["skipped"]))


def test_report_parse() -> None:
    print("\n[摘要解析]")
    payload = {"summary": "群里通知周五停电。", "points": ["周五全天停电", "服务器提前关机"],
               "todos": [{"title": "关机", "owner": "张三"}]}
    got = llm.parse_report(json.dumps(payload, ensure_ascii=False))
    check("summary 解析", got and got["summary"].startswith("群里通知"))
    check("points 解析", got and len(got["points"]) == 2)
    check("todos 解析", got and got["todos"][0]["title"] == "关机")
    check("只有待办也能解析", llm.parse_report('{"todos":[]}') ==
          {"summary": "", "points": [], "todos": []})

    msgs = [{"role": "user", "content": [
        {"type": "text", "text": "记录"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}}]}]
    check("识别带图消息", llm.has_images(msgs) is True)
    stripped = llm.strip_images(msgs)
    check("降级后没有图片", llm.has_images(stripped) is False)
    check("降级后保留原文", "记录" in stripped[0]["content"])


def test_mode() -> None:
    print("\n[产出模式]")
    check("all", schemas.parse_mode("all") == "all")
    check("digest", schemas.parse_mode("digest") == "digest")
    check("摘要（别名）", schemas.parse_mode("摘要") == "digest")
    check("todo", schemas.parse_mode("todo") == "todo")
    check("乱写回落默认", schemas.parse_mode("xxx", "all") == "all")
    check("digest 动作", schemas.parse_spec(["digest", "24h"]) == ("digest", ["24h"]))
    check("todo 动作", schemas.parse_spec(["todo"]) == ("todo", []))


def test_llm_config() -> None:
    """密钥清洗 / 接口规范化 / 脱敏 —— 都是纯函数，不碰网络。"""
    print("\n[模型配置]")
    clean = cmd_main._clean_key
    check("去引号", clean('"sk-1234567890"') == "sk-1234567890")
    check("去 Bearer 前缀", clean("Bearer sk-1234567890") == "sk-1234567890")
    check("去空格与零宽", clean(" sk-1234 567890\u200b ") == "sk-1234567890")
    check("太短的拒收", clean("abc") == "")
    check("空串拒收", clean("") == "")
    check("中文注释不算", clean("我还没申请key") == "")

    clean_url = cmd_main._clean_base_url
    check("标准地址", clean_url("https://api.deepseek.com/v1") ==
          "https://api.deepseek.com/v1")
    check("省 scheme 补 https", clean_url("api.siliconflow.cn/v1") ==
          "https://api.siliconflow.cn/v1")
    check("去尾斜杠", clean_url("https://api.deepseek.com/v1/") ==
          "https://api.deepseek.com/v1")
    check("本地端口保留", clean_url("http://127.0.0.1:8000/v1") ==
          "http://127.0.0.1:8000/v1")
    check("localhost 也认", clean_url("localhost:11434/v1") ==
          "https://localhost:11434/v1")
    check("乱写拒收", clean_url("这不是地址") is None)
    check("裸词拒收", clean_url("hello") is None)
    check("ftp 拒收", clean_url("ftp://a.com/v1") is None)
    check("端口越界拒收", clean_url("http://a.com:99999/v1") is None)

    mask = render.mask_key
    check("长 key 只留前四后四", mask("sk-abcdef1234567890") == "sk-a…7890（19 位）",
          mask("sk-abcdef1234567890"))
    check("短 key 打星", mask("sk-123456") == "sk" + "*" * 7, mask("sk-123456"))
    check("空 key 空串", mask("") == "")


def test_qa() -> None:
    """问答的检索与解析 —— 重点验证「放不下时不会把答案抽掉」。"""
    print("\n[问答]")
    now = 1_800_000_000
    span = qa.detect_span("这周有什么要交的", now=now)
    check("问题里的「这周」→ 周窗口", span is not None and span[1] == now, str(span))
    span = qa.detect_span("最近3天谁请假了", now=now)
    check("「最近3天」→ 72 小时", span is not None and span[1] - span[0] == 3 * 86400,
          str(span))
    span = qa.detect_span("昨天开会说了什么", now=now)
    check("「昨天」→ 昨天整段", span is not None and span[1] - span[0] == 86400)
    check("没提时间返回 None", qa.detect_span("谁负责收表", now=now) is None)

    check("二元组切分", qa.grams("开题报告") == {"开题", "题报", "报告"},
          str(qa.grams("开题报告")))
    qgrams = qa.question_grams("开题报告什么时候交")
    check("疑问词不参与打分", "什么" not in qgrams and "开题" in qgrams, str(qgrams))

    base = now
    rows = [{"message_id": i, "sender": f"用户{i % 5}",
             "text": f"闲聊第{i}条，随便说两句天气和食堂", "ts": base + i, "media": []}
            for i in range(300)]
    rows.append({"message_id": 999, "sender": "学习委员",
                 "text": "开题报告下周一前交到教务系统", "ts": base + 301, "media": []})
    cfg = {"noise": [], "min_len": 1, "merge_seconds": 0, "max_msg_chars": 500}

    picked = qa.select(rows, "开题报告什么时候交", clean_cfg=cfg, max_chars=2000)
    check("放不下就筛", picked["trimmed"] is True and picked["picked"] < picked["total"],
          f"{picked['picked']}/{picked['total']}")
    check("答案那条被留下（不是均匀抽稀）",
          any("开题报告" in item["text"] for item in picked["items"]),
          str([item["text"][:16] for item in picked["items"]][:3]))
    check("筛完仍按时间排",
          [int(i["ts"]) for i in picked["items"]] ==
          sorted(int(i["ts"]) for i in picked["items"]))

    small = qa.select(rows[:5], "随便", clean_cfg=cfg, max_chars=100000)
    check("放得下就全给", small["trimmed"] is False and small["picked"] == 5)

    got = qa.parse_answer('{"answer":"周五开会","refs":[1,"2",0,-1,3],"enough":true}')
    check("解析 answer", bool(got) and got["answer"] == "周五开会")
    check("refs 过滤非法值", bool(got) and got["refs"] == [1, 2, 3],
          str(got["refs"] if got else None))
    got = qa.parse_answer('```json\n{"answer":"包在围栏里","refs":[]}\n```')
    check("去代码围栏", bool(got) and got["answer"] == "包在围栏里")
    check("纯文本返回 None", qa.parse_answer("周五开会，别迟到") is None)
    check("strip_fence 清理", qa.strip_fence("```\nx\n```") == "x")

    first = picked["items"][0]["no"]
    cites = qa.cite_items(picked, [first])
    check("引用能对上消息", len(cites) == 1 and cites[0]["no"] == first)
    check("不存在的编号忽略", qa.cite_items(picked, [99999]) == [])


def test_incremental() -> None:
    """增量窗口：接上次、截断、关闭、空窗口，以及对提问不使用增量。"""
    print("\n[增量窗口]")
    from qqnotice_sum import service

    base = 1_800_000_000

    class P:
        on = True

        def incremental(self):
            return self.on

        def window_hours(self, gid):
            return 24

        def max_incremental_hours(self):
            return 168

    p = P()
    original = store.last_run
    try:
        store.last_run = lambda gid: None
        plan = service.plan_window(p, 1, now=base)
        check("首次总结回落到 window_hours",
              plan["end_ts"] - plan["start_ts"] == 24 * 3600)
        check("首次不算增量", plan["incremental"] is False, str(plan))

        class R1:
            end_ts = base - 3600
        store.last_run = lambda gid: R1()
        plan = service.plan_window(p, 1, now=base)
        # +1 秒：避开「同一秒里总结完又收到一条」的边界重复
        check("增量从上次结束时刻（+1 秒）开始",
              plan["start_ts"] == base - 3600 + 1, str(plan))
        check("标的增量且记住 since",
              plan["incremental"] is True and plan["since_ts"] == base - 3599, str(plan))

        class R2:
            end_ts = base - 30 * 86400
        store.last_run = lambda gid: R2()
        plan = service.plan_window(p, 1, now=base)
        check("跨度超上限就截断",
              plan["end_ts"] - plan["start_ts"] == 168 * 3600, str(plan))
        check("截断时标 capped", plan["capped"] is True, str(plan))

        class R3:
            end_ts = base
        store.last_run = lambda gid: R3()
        plan = service.plan_window(p, 1, now=base)
        check("刚总结过给空窗口",
              plan["start_ts"] == plan["end_ts"] == base, str(plan))
        # 空窗口也要认成增量 —— 否则界面上会显示「插件刚装没收到消息」这种误导
        check("空窗口仍标为增量", plan["incremental"] is True, str(plan))

        p.on = False
        plan = service.plan_window(p, 1, now=base)
        check("关掉增量退回固定窗口",
              plan["end_ts"] - plan["start_ts"] == 24 * 3600, str(plan))
        check("关掉增量不标增量", plan["incremental"] is False, str(plan))
        p.on = True

        store.last_run = lambda gid: R3()
        start, end = service.window_full(p, 1, now=base)
        check("提问用固定窗口（不受增量影响）", end - start == 24 * 3600,
              f"{end - start}")

        check("sync 只要范围也走增量",
              service.window_for(p, 1, now=base)[0] == base)
    finally:
        store.last_run = original


async def test_media_api() -> None:
    """附件必须走框架的流式下载，而不是自己直连 URL。"""
    print("\n[附件走框架 API]")
    calls = []

    class Api:
        async def download_file_image_stream(self, *, file=None, file_id=None,
                                             timeout=60.0, **kw):
            calls.append(("image", file, file_id))
            yield b"\x89PNG\r\n\x1a\n" + b"pixels"

        async def download_file_stream(self, *, file=None, file_id=None,
                                       timeout=60.0, **kw):
            calls.append(("file", file, file_id))
            yield "明天交表".encode("utf-8")

    class P:
        api = Api()

    cfg = {"enabled": True, "timeout": 5, "cache_hours": 0,
           "image": {"enabled": True, "max_bytes": 1024 * 1024, "max_per_run": 4},
           "file": {"enabled": True, "max_bytes": 1024 * 1024, "max_per_run": 4,
                    "max_chars": 500, "allow_ext": [".txt"]}}

    items = [
        {"kind": "image", "label": "图1", "index": 0,
         "descriptor": {"file": "a.jpg", "file_id": "img-1"},
         "row": {"message_id": 1}},
        {"kind": "file", "label": "文件1", "index": 0,
         "descriptor": {"name": "名单.txt", "file": "f-1",
                        "url": "http://127.0.0.1:1/never"},
         "row": {"message_id": 2}},
    ]
    out = await media.gather(P(), 1, items, cfg=cfg)
    check("图片走 image_stream", bool(calls) and calls[0][0] == "image", str(calls))
    check("图片拿成 data URI",
          bool(out["images"])
          and out["images"][0]["data_uri"].startswith("data:image/png;base64,"),
          str(out["images"])[:80])
    check("文件走 file_stream", any(c[0] == "file" for c in calls), str(calls))
    check("文件解出正文",
          bool(out["files"]) and out["files"][0]["status"] == "ok"
          and "明天交表" in out["files"][0]["text"], str(out["files"])[:120])

    # 全失败时要说清原因，不能静默跳过
    class BadApi:
        async def download_file_image_stream(self, **kw):
            raise RuntimeError("napcat 550")
            yield b""

    class P2:
        api = BadApi()

    out = await media.gather(P2(), 1, [items[0]], cfg=cfg)
    check("失败给出原因", not out["images"]
          and any("550" in note for note in out["notes"]), str(out["notes"]))

    # 超过上限时必须在拿完之前就停（_drain 的兜底）
    class BigApi:
        async def download_file_image_stream(self, *, file=None, file_id=None,
                                             timeout=60.0, **kw):
            for _ in range(4):
                yield b"x" * 1024

    class P3:
        api = BigApi()

    tiny = {**cfg, "image": {**cfg["image"], "max_bytes": 1024}}
    out = await media.gather(P3(), 1, [items[0]], cfg=tiny)
    check("流式超限就丢", not out["images"]
          and any("超过大小上限" in note for note in out["notes"]), str(out["notes"]))


def test_upgrade() -> None:
    """老库（1.1.0 没有 media/points 列）能不能原地升级。"""
    print("\n[老库升级]")
    import sqlite3
    from napcat.store import Database

    tmp = Path(tempfile.mkdtemp(prefix="qqnotice-sum-upgrade-")) / "old.db"
    con = sqlite3.connect(str(tmp))
    con.executescript("""
        CREATE TABLE qs_msg (id INTEGER PRIMARY KEY AUTOINCREMENT, group_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL UNIQUE, seq INTEGER NOT NULL DEFAULT 0,
            user_id INTEGER NOT NULL DEFAULT 0, sender TEXT NOT NULL DEFAULT '',
            text TEXT NOT NULL DEFAULT '', kind TEXT NOT NULL DEFAULT 'text',
            ts INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE qs_run (id INTEGER PRIMARY KEY AUTOINCREMENT, group_id INTEGER NOT NULL,
            start_ts INTEGER NOT NULL DEFAULT 0, end_ts INTEGER NOT NULL DEFAULT 0,
            msg_count INTEGER NOT NULL DEFAULT 0, todo_new INTEGER NOT NULL DEFAULT 0,
            todo_open INTEGER NOT NULL DEFAULT 0, model TEXT NOT NULL DEFAULT '',
            cost_ms INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL DEFAULT 0);
        INSERT INTO qs_msg (group_id, message_id, text, ts) VALUES (1, 100, '老消息', 1);
        INSERT INTO qs_run (group_id, created_at) VALUES (1, 1);
    """)
    con.commit()
    con.close()

    db = Database(tmp).register(store.Msg, store.Todo, store.Run,
                                store.SyncState, store.Media)
    check("老数据没丢", store.Msg.objects.filter(group_id=1).count() == 1)
    check("新列 media 可读", store.Msg.objects.filter(group_id=1).first().media is None)
    store.save_messages(db, 1, [{"message_id": 101, "text": "新消息", "media": [], "ts": 2}])
    check("新消息能写", store.message_count(1) == 2)
    check("老 run 能读", store.last_run(1).created_at == 1)


# ------------------------------------------------------------------ 端到端

def _group_event(text: str = "", *, mid: int = 1, uid: int = MEMBER,
                 gid: int = GROUP, role: str = "owner") -> MessageEvent:
    """群消息（采集用）。"""
    return MessageEvent(
        time=int(time.time()), self_id=SUPER, post_type="message",
        message_type="group", message_id=mid, message_seq=mid,
        user_id=uid, group_id=gid,
        sender=Sender(user_id=uid, nickname=f"用户{uid}", card=f"用户{uid}", role=role),
        message=[{"type": "text", "data": {"text": text}}] if text else [],
    )


def _group_event_raw(segments: list, *, mid: int = 1, uid: int = MEMBER,
                     gid: int = GROUP) -> MessageEvent:
    """带指定消息段的群消息（测图片/文件用）。"""
    return MessageEvent(
        time=int(time.time()), self_id=SUPER, post_type="message",
        message_type="group", message_id=mid, message_seq=mid,
        user_id=uid, group_id=gid,
        sender=Sender(user_id=uid, nickname=f"用户{uid}", card=f"用户{uid}", role="owner"),
        message=segments,
    )


def _private_event(text: str = "x", *, mid: int = 1, uid: int = SUPER) -> MessageEvent:
    """私聊消息（发命令用）。"""
    return MessageEvent(
        time=int(time.time()), self_id=SUPER, post_type="message",
        message_type="private", message_id=mid, message_seq=mid, user_id=uid,
        sender=Sender(user_id=uid, nickname="admin"),
        message=[{"type": "text", "data": {"text": text}}],
    )


class _FakeApi:
    """模拟 NapCat 的 `get_group_msg_history`。

    语义与真接口一致：`reverseOrder=True` 时返回**新→旧**；`message_seq` 是
    「从这条往前（更旧）翻」的游标；数据按时间升序存着。
    """

    def __init__(self, messages: list[dict]):
        self.all = messages

    async def get_group_msg_history(self, group_id, *, count: int = 20,
                                    message_seq=None, reverse_order: bool = False):
        items = self.all
        if message_seq:
            index = next((i for i, m in enumerate(items)
                          if m["message_seq"] == message_seq), len(items))
            items = items[:index]               # 只要比游标更旧的
        return {"messages": list(reversed(items[-int(count):]))}


async def _cmd(plugin, args: list, event=None) -> str:
    """跑一次命令，返回文本（`_out` 短文本回字符串）。"""
    params = SumSchema().validate(list(args), {})
    result = await cmd_main.cmd_sum(plugin, params, event or _private_event())
    if isinstance(result, list):
        return "\n".join(str(x) for x in result)
    return result if isinstance(result, str) else ""


async def test_end_to_end() -> None:
    print("\n[端到端]")
    tmp = Path(tempfile.mkdtemp(prefix="notice-todo-test-"))
    (tmp / "plugin").mkdir(parents=True, exist_ok=True)
    cfg = ConfigManager(str(tmp / "config.json"))
    bus, timer = EventBus(), TimerManager()
    timer.start()
    perm = PermissionManager(superusers=[SUPER])
    cmds = CommandManager(prefixes=["/"], perm_manager=perm)
    logger = logging.getLogger("notice-todo-test")

    plugin = QQNoticeSumPlugin(
        api=None, event_bus=bus, timer=timer, cmd_manager=cmds, perm_manager=perm,
        file_manager=FileManager(api=None), logger=logger,
        groups=GroupBinding(bound=set(), all_groups=True), cfg=cfg,
        client=None, error_reporter=ErrorReporter(logger=logger, perm=perm),
        plugin_dir=str(tmp / "plugin"),
    )
    await plugin.on_load()
    await plugin.on_enable()
    cmd_main.COOLDOWN_SECONDS = 0            # 测试里连跑几次总结，不等冷却
    cmd_main.ASK_COOLDOWN = 0                # 连问几个问题，同样不等

    # ---- 命令注册：权限与作用域必须是框架声明，而不是插件自己判
    handlers = cmds.list_commands()
    handler = handlers.get("sum")
    check("命令已注册", handler is not None)
    check("permission=admin（框架层声明）", getattr(handler, "permission", "") == "admin",
          str(getattr(handler, "permission", "")))
    check("scope=private（只在私聊响应）", getattr(handler, "scope", "") == "private",
          str(getattr(handler, "scope", "")))
    check("中文别名 /待办", "待办" in handlers)
    check("英文别名 /td", "td" in handlers)

    # ---- 托管群
    reply = await _cmd(plugin, ["config", "add", str(GROUP)])
    check("config add 回执", "已托管" in reply, reply[:60])
    check("群号进了托管名单", plugin.is_watched(GROUP) is True)

    # ---- 喂消息（真实入库；采集走群事件）
    texts = ["明天下午五点前把开题报告交到教务系统，@张三 负责",
             "收到", "还有周五下午三点在 302 开会，所有人必须到"]
    for i, text in enumerate(texts, start=10):
        await msg_event.record(plugin, _group_event(text, mid=i))
    check("消息落库 3 条", store.message_count(GROUP) == 3,
          f"实际 {store.message_count(GROUP)}")
    await msg_event.record(plugin, _group_event(texts[0], mid=10))
    check("重复 message_id 去重", store.message_count(GROUP) == 3)

    # 带图的消息：占位符要变成 [图1]，并进模型上下文
    await msg_event.record(plugin, _group_event_raw([
        {"type": "text", "data": {"text": "课表看图"}},
        {"type": "image", "data": {"file": "abc.jpg", "url": "http://127.0.0.1:1/x.jpg"}},
    ], mid=13))
    check("带图消息也入库", store.message_count(GROUP) == 4,
          f"实际 {store.message_count(GROUP)}")

    # ---- 假模型
    calls = []

    async def fake_chat(p, messages, *, model=""):
        calls.append(messages)
        return json.dumps({"summary": "群里通知两件事：交开题报告、周五开会。",
                           "points": ["开题报告明天 17:00 前交", "周五 15:00 在 302 开会"],
                           "todos": [
            {"title": "提交开题报告", "owner": "张三", "due": "2026-11-05 17:00",
             "priority": "high", "evidence": "明天下午五点前交"},
            {"title": "参加 302 会议", "owner": "", "due": "2026-11-06 15:00",
             "priority": "normal", "evidence": "必须到"},
        ]}, ensure_ascii=False)

    original_chat = llm.chat
    llm.chat = fake_chat
    try:
        # 只托管一个群 → 群号可省
        reply = await _cmd(plugin, ["summary", "24h"])
        check("单群时群号可省", "提交开题报告" in reply, reply[:120])
        check("总结带截止时间", "截止 2026-11-05 17:00" in reply)
        check("报告含【概要】", "【概要】" in reply, reply[:80])
        check("报告含【要点】", "【要点】" in reply)
        check("报告含【待办】", "【待办】" in reply)

        digest_reply = await _cmd(plugin, ["digest", "24h"])
        check("digest 只有摘要", "【概要】" in digest_reply and "【待办】" not in digest_reply,
              digest_reply[:80])
        todo_reply = await _cmd(plugin, ["todo", "24h"])
        check("todo 只有待办", "【待办】" in todo_reply and "【概要】" not in todo_reply,
              todo_reply[:80])
        check("模型收到了群消息", calls and "开题报告" in calls[0][1]["content"])
        check("图片被标成 [图1]", bool(calls) and "[图1]" in calls[0][1]["content"],
              (calls[0][1]["content"][:120] if calls else ""))
        check("待办入库 2 条", len(store.open_todos(GROUP)) == 2)

        await _cmd(plugin, ["summary", "24h"])
        check("重复总结不新增", len(store.open_todos(GROUP)) == 2,
              f"实际 {len(store.open_todos(GROUP))}")

        # 增量：不给窗口时接着上次，刚总结过就该说「没有新消息」而不是再跑一遍
        reply = await _cmd(plugin, ["summary"])
        check("增量：刚总结过提示没有新消息", "没有新消息" in reply, reply[:80])

        async def bad_chat(p, messages, *, model=""):
            return "这段时间大概有三件事要做吧"
        llm.chat = bad_chat
        reply = await _cmd(plugin, ["summary", "24h"])
        check("未结构化时给出原文提示", "没按格式输出" in reply, reply[:80])
    finally:
        llm.chat = original_chat

    # ---- 提问（只读）
    async def qa_chat(p, messages, *, model=""):
        calls.append(messages)
        return json.dumps({"answer": "开题报告明天 17:00 前交给张三。",
                           "refs": [1], "enough": True}, ensure_ascii=False)

    original_chat_qa = llm.chat
    open_before = len(store.open_todos(GROUP))
    try:
        llm.chat = qa_chat
        reply = await _cmd(plugin, ["ask", "开题报告什么时候交"])
        check("ask 有答案", "开题报告明天" in reply, reply[:100])
        check("ask 回显问题", "💬 开题报告什么时候交" in reply, reply[:60])
        check("ask 带依据", "依据" in reply and "1）" in reply, reply[:150])
        check("ask 不产生待办", len(store.open_todos(GROUP)) == open_before,
              f"{open_before} → {len(store.open_todos(GROUP))}")
        body = calls[-1][-1]["content"] if calls else ""
        check("记录带编号喂给模型", "[1]" in body, body[:120])
        check("问题在 prompt 里", "问题：开题报告什么时候交" in body, body[-120:])

        # 不带 ask，直接把问题当命令发
        reply = await _cmd(plugin, ["谁负责收表"])
        check("不带 ask 也能直接问", "开题报告明天" in reply, reply[:80])

        # 问题里带时间词 → 自动放宽窗口
        reply = await _cmd(plugin, ["ask", "本周有什么要交的"])
        check("「本周」被认出来", "本周有什么要交的" in reply and "范围" in reply,
              reply[:100])

        reply = await _cmd(plugin, ["ask"])
        check("空问题给用法", "/sum ask 这周有什么要交的" in reply, reply[:120])

        cmd_main.ASK_COOLDOWN = 3
        plugin._last_ask[GROUP] = int(time.time())
        reply = await _cmd(plugin, ["ask", "还在吗"])
        check("追问也有冷却", "秒后再问" in reply, reply[:60])
        cmd_main.ASK_COOLDOWN = 0

        # 模型没按 JSON 回：原文当答案，不假装有引用
        async def plain_chat(p, messages, *, model=""):
            return "```\n开题报告明天交\n```"
        llm.chat = plain_chat
        reply = await _cmd(plugin, ["ask", "开题报告什么时候交"])
        check("非 JSON 也照原文显示", "开题报告明天交" in reply and "```" not in reply,
              reply[:120])
    finally:
        llm.chat = original_chat_qa

    # ---- 冷却
    cmd_main.COOLDOWN_SECONDS = 60
    plugin._last_summary[GROUP] = int(time.time())
    reply = await _cmd(plugin, ["summary", "24h"])
    check("连点会被冷却拦住", "秒后再试" in reply, reply[:60])
    cmd_main.COOLDOWN_SECONDS = 0

    # ---- 列表 / 完成
    reply = await _cmd(plugin, ["list"])
    check("列表带编号", "#" in reply and "提交开题报告" in reply, reply[:120])
    first = store.open_todos(GROUP)[-1]
    reply = await _cmd(plugin, ["done", str(first["id"])])
    check("done 标记完成", "已完成" in reply, reply[:60])
    check("完成后不在 open 列表", len(store.open_todos(GROUP)) == 1)
    reply = await _cmd(plugin, ["list", "all"])
    check("list all 含已完成", "已完成" in reply, reply[:100])
    reply = await _cmd(plugin, ["done", str(first["id"])])
    check("重复完成有提示", "已经处理过" in reply)

    # ---- 多群时要求带群号
    await _cmd(plugin, ["config", "add", str(GROUP2)])
    reply = await _cmd(plugin, ["summary", "24h"])
    check("多群时提示带群号", "要指定群号" in reply, reply[:80])
    llm.chat = fake_chat
    try:
        reply = await _cmd(plugin, ["summary", str(GROUP), "24h"])
        check("带群号能跑", "提交开题报告" in reply or "没有采集到消息" in reply,
              reply[:80])
    finally:
        llm.chat = original_chat

    # ---- 配置
    reply = await _cmd(plugin, ["config", "window", str(GROUP), "12h"])
    check("config window", plugin.window_hours(GROUP) == 12, reply[:60])
    reply = await _cmd(plugin, ["config", "time", "21:30"])
    check("config time（单群默认？多群时提示）",
          "21:30" in reply or "要指定群号" in reply, reply[:60])
    reply = await _cmd(plugin, ["config", "add", str(GROUP2)])
    check("重复加群有提示", "本来就在托管" in reply, reply[:60])

    reply = await _cmd(plugin, ["status"])
    check("status 有库路径", "数据库" in reply and "qqnotice_sum.db" in reply, reply[:200])
    reply = await _cmd(plugin, [])
    check("面板有用法", "/sum summary" in reply and "托管" in reply, reply[:120])
    reply = await _cmd(plugin, ["config"])
    check("config 总览", "/sum config" in reply and "add" in reply)

    # ---- 补采（假接口）
    base = int(time.time())
    raw = [{"message_id": 5000 + i, "message_seq": 5000 + i,
            "time": base - (45 - i) * 60,
            "sender": {"user_id": 7, "nickname": "历史"},
            "message": [{"type": "text", "data": {"text": f"历史消息 {i}"}}]}
           for i in range(45)]
    plugin.api = _FakeApi(raw)
    before = store.message_count(GROUP)
    saved = await history.backfill(plugin, GROUP, since_ts=base - 40 * 60,
                                   max_pages=5, page_size=20)
    check("补采只拿窗口内的（40 条）", saved == 40, f"实际 {saved}")
    check("补采真的落库", store.message_count(GROUP) == before + 40,
          f"实际 {store.message_count(GROUP)}")
    saved_again = await history.backfill(plugin, GROUP, since_ts=base - 40 * 60,
                                         max_pages=5, page_size=20)
    check("重复补采不重复入库", saved_again == 0, f"实际 {saved_again}")
    plugin.api = None

    # ---- 模型配置（私聊 key / api）
    class _RecallApi:
        def __init__(self):
            self.deleted = []

        async def delete_msg(self, message_id):
            self.deleted.append(int(message_id))

    recall_api = _RecallApi()
    plugin.api = recall_api

    reply = await _cmd(plugin, ["config", "key", "sk-should-not-work"],
                       _private_event(uid=MEMBER))
    check("非超管配 key 被拒", "只有超管" in reply, reply[:60])
    check("非超管没写进配置", not plugin.llm_cfg()["api_key"])

    reply = await _cmd(plugin, ["config", "key"], _private_event(uid=SUPER))
    check("没配时提示怎么配", "/sum config key" in reply, reply[:80])

    secret = "sk-abcdef1234567890abcdef"
    reply = await _cmd(plugin, ["config", "key", f'"{secret}"'], _private_event(uid=SUPER))
    check("key 保存成功", plugin.llm_cfg()["api_key"] == secret, reply[:80])
    check("回复里不回显完整 key", secret not in reply, reply[:120])
    check("回复里是脱敏形态", "sk-a" in reply and "cdef（" in reply, reply[:80])
    check("含 key 的消息被撤回", recall_api.deleted == [1], str(recall_api.deleted))

    reply = await _cmd(plugin, ["config", "key"], _private_event(uid=SUPER))
    check("无参显示脱敏 key", secret not in reply and "sk-a" in reply, reply[:80])

    # 一条命令配两样：key + 接口
    reply = await _cmd(plugin, ["config", "key", "sk-newkey0987654321",
                                "api.siliconflow.cn/v1"], _private_event(uid=SUPER))
    check("同时设了接口", plugin.llm_cfg()["base_url"] == "https://api.siliconflow.cn/v1",
          plugin.llm_cfg()["base_url"])
    check("省 scheme 也认", "https://" in reply, reply[:100])

    reply = await _cmd(plugin, ["config", "api", "http://127.0.0.1:8000/v1/"],
                       _private_event(uid=SUPER))
    check("api 去尾斜杠", plugin.llm_cfg()["base_url"] == "http://127.0.0.1:8000/v1",
          plugin.llm_cfg()["base_url"])
    reply = await _cmd(plugin, ["config", "api", "这不是地址"], _private_event(uid=SUPER))
    check("乱写的接口被拒", "不像接口地址" in reply, reply[:60])

    reply = await _cmd(plugin, ["config", "api", "clear"], _private_event(uid=SUPER))
    check("api clear 恢复默认", plugin.llm_cfg()["base_url"] == cmd_main.DEFAULT_BASE_URL,
          plugin.llm_cfg()["base_url"])

    # test 动作：成功与失败两条路
    original_chat2 = llm.chat

    async def ok_chat(p, messages, *, model=""):
        return "ok"

    async def boom_chat(p, messages, *, model=""):
        from napcat.errors import PluginConfigError
        raise PluginConfigError("bad key", user_message="模型密钥无效或没权限")

    try:
        llm.chat = ok_chat
        reply = await _cmd(plugin, ["config", "test"], _private_event(uid=SUPER))
        check("test 调通有耗时", "调通了" in reply and "ms" in reply, reply[:100])
        llm.chat = boom_chat
        reply = await _cmd(plugin, ["config", "test"], _private_event(uid=SUPER))
        check("test 失败说原因", "调不通" in reply and "密钥无效" in reply, reply[:120])
    finally:
        llm.chat = original_chat2

    reply = await _cmd(plugin, ["config", "key", "clear"], _private_event(uid=SUPER))
    check("key clear 清空", plugin.llm_cfg()["api_key"] == "", reply[:60])
    reply = await _cmd(plugin, ["config", "test"], _private_event(uid=SUPER))
    check("没 key 时 test 提示先配", "还没配密钥" in reply, reply[:60])

    plugin.api = None

    # ---- 取消托管
    reply = await _cmd(plugin, ["config", "del", str(GROUP2)])
    check("config del 生效", plugin.is_watched(GROUP2) is False, reply[:60])

    timer.stop()


def main() -> int:
    test_spec()
    test_window()
    test_misc_parsers()
    test_collect()
    test_digest()
    test_llm_parse()
    test_media()
    asyncio.run(test_media_limits())
    test_report_parse()
    test_mode()
    test_llm_config()
    test_qa()
    test_incremental()
    asyncio.run(test_media_api())
    test_upgrade()
    try:
        asyncio.run(test_end_to_end())
    except Exception:                          # noqa: BLE001 - 测试脚本：打印后继续统计
        traceback.print_exc()
        check("端到端未抛异常", False)

    print(f"\n{'=' * 46}")
    print(f"通过 {len(_PASSED)} 项，失败 {len(_FAILED)} 项")
    if _FAILED:
        for name in _FAILED:
            print(f"  ✗ {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
