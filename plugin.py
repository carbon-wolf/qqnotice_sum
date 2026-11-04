# -*- coding: utf-8 -*-
"""qqnotice_sum —— 通知群消息 → 待办清单。

这个模块**只有装配**：插件元信息、配置模板、生命周期。业务逻辑在：

    commands/main.py   唯一一条命令 `/sum` 的动作分发
    events/message.py  实时采集
    jobs/push.py       到点推送 / 停机补漏 / 历史清理
    collect.py         消息段 → 文本
    history.py         群历史消息补采
    digest.py          清洗、合并、预算裁剪
    qa.py              提问：按问题筛消息 + 容错解析回答
    llm.py             OpenAI 兼容接口 + JSON 容错解析
    render.py          面板 / 列表 / 总结 / 回答的文案
    store.py           五张表的模型

数据全部走**框架提供的 sqlite ORM**：`napcat.store.Model` 声明表结构，
`self.database(ensure=[...])` 拿库（文件落在 `plugins/qqnotice_sum/data/`，
卸载/热重载由框架统一 close）。插件里没有任何 `sqlite3.connect`。

权限也全交给框架：命令用 `permission=` / `scope=` 声明，运行时可以用
`/perm` 与 `/userperm` 改，插件不自己发明等级。

设计约束（来自 docs/plugin-dev/14-layout.md）：

* `__init__.py` 里必须写 `PLUGIN`，否则框架可能实例化到导入进来的基类；
* 包内一律相对导入（框架加载时模块名是 `napcat_plugin_qqnotice_sum`）；
* 状态放实例属性 / 数据库 / 配置，不放模块全局（热重载会重新执行模块）。
"""
from __future__ import annotations

import os
from pathlib import Path

from napcat.plugin import PluginBase

from .store import Media, Msg, Run, SyncState, Todo

DEFAULT_WINDOW_HOURS = 24
DEFAULT_PUSH_TIME = "21:30"
DEFAULT_MAX_TODOS = 30
DEFAULT_RETENTION_DAYS = 60
DEFAULT_MAX_INCREMENTAL_HOURS = 168        # 增量最多往回补 7 天

#: 命令名与别名（`/help` 与文档都按这个口径写）
COMMAND_NAME = "sum"


class QQNoticeSumPlugin(PluginBase):
    name = "qqnotice_sum"
    version = "1.6.0"
    description = "通知群消息 → 摘要 / 待办 / 随时提问：实时采集 + 历史补采，私聊可用，每日推送"
    author = "carbon-wolf"

    # ------------------------------------------------------------------ 生命周期

    async def on_load(self):
        """建目录、建库、准备状态位。**不要**在这里调 API（WS 还没连上）。"""
        self._bootstrapped: set[int] = set()      # 本进程已补过历史采的群
        self._pushing: set[int] = set()           # 正在推送的群（防重入）
        self._last_summary: dict[int, int] = {}   # 手动总结的冷却时刻
        self._last_ask: dict[int, int] = {}       # 追问的冷却时刻（比总结短）
        self._last_catchup: dict[int, int] = {}   # 停机补漏的节流时刻
        self._pending_sync: dict[int, tuple] = {}  # 实时采集攒下的游标
        self._last_cleanup = ""                   # 上次清理的日期
        try:
            os.makedirs(Path(self.plugin_dir) / "data", exist_ok=True)
        except Exception as e:                    # noqa: BLE001 - 目录建不出来也不该让插件消失
            self.logger.warning("[qqnotice_sum] 建 data 目录失败：%s", e)
        # 框架提供的 sqlite 库：注册模型 = 建表 + 绑定，之后 Model.objects 就能用
        self.database(ensure=[Msg, Todo, Run, SyncState, Media])
        self.logger.info("[qqnotice_sum] 已加载，托管群 %d 个", len(self.group_ids()))


    async def on_enable(self):
        """只做装配：命令 / 事件 / 定时任务各归各的模块。"""
        from .commands import main as command_module
        from .events import message as message_module
        from .jobs import push as push_module

        self.register_module(command_module, message_module, push_module)
        self._register_ready_hook()

    # ------------------------------------------------------------------ 配置

    def config_template(self) -> dict:
        """完整配置模板（框架只在加载时**补缺**，不覆盖已有值）。

        `groups` 的键就是「要采集/推送哪些群」；谁能用命令由命令上的
        `permission` 决定（框架的权限系统），配置里不再维护第二份名单。
        """
        return {
            "enabled": True,
            "llm": {
                "base_url": "https://api.deepseek.com/v1",
                "api_key": "",
                "model": "deepseek-chat",
                "temperature": 0.2,
                "timeout": 120,
                "max_context_chars": 14000,
            },
            "defaults": {
                "window_hours": DEFAULT_WINDOW_HOURS,
                "push_time": DEFAULT_PUSH_TIME,
                "max_todos": DEFAULT_MAX_TODOS,
                "mode": "all",              # all / digest / todo
                # 增量总结：从上一次总结结束的时刻开始，只处理新消息。
                # 关掉就每次回溯固定 window_hours。
                "incremental": True,
                # 增量的上限：停机一个月后不该一次塞爆上下文
                "max_incremental_hours": 168,
            },
            # 群号 → 该群的采集/推送设置。空 = 一个群都不托管。
            "groups": {},
            # 图片与附件的取用：默认开，但都有硬上限 —— 一个 200MB 的压缩包
            # 不该把内存和 token 一起吃掉。
            "media": {
                "enabled": True,
                "timeout": 45,
                "cache_hours": 72,
                "image": {
                    "enabled": True,
                    "max_bytes": 3 * 1024 * 1024,
                    "max_per_run": 8,
                },
                "file": {
                    "enabled": True,
                    "max_bytes": 10 * 1024 * 1024,
                    "max_per_run": 3,
                    "max_chars": 6000,
                    "allow_ext": [
                        ".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".log",
                        ".ini", ".cfg", ".yaml", ".yml", ".xml", ".html", ".htm",
                        ".pdf", ".docx", ".xlsx",
                    ],
                },
            },
            "clean": {
                "noise": ["收到", "好的", "谢谢", "1", "ok", "嗯嗯", "打卡", "已阅"],
                "min_len": 2,
                "merge_seconds": 300,
                "max_msg_chars": 500,
                "retention_days": DEFAULT_RETENTION_DAYS,
            },
            "history": {
                "bootstrap_hours": 24,
                "max_pages": 10,
                "page_size": 40,
            },
        }

    # ---- 读配置的小工具（每个都带兜底，配错了不该炸）

    def is_on(self) -> bool:
        """插件总开关（`plugins.qqnotice_sum.enabled`）。

        ⚠️ 名字不能叫 `enabled` —— `PluginBase.enabled` 是框架用来判断插件
        是否启用的属性，同名方法会把它盖掉（框架里 `plugin.enabled` 就变成
        一个 bound method，所有判断恒为真）。
        """
        return bool(self.config.get("enabled", True))

    def groups(self) -> dict:
        raw = self.config.get("groups")
        return raw if isinstance(raw, dict) else {}

    def group_ids(self) -> set[int]:
        """托管群号集合（容错：键写成字符串/整数都认）。"""
        out: set[int] = set()
        for key in self.groups():
            try:
                gid = int(key)
            except (TypeError, ValueError):
                continue
            if gid > 0:
                out.add(gid)
        return out

    def group_cfg(self, group_id) -> dict:
        try:
            key = str(int(group_id))
        except (TypeError, ValueError):
            return {}
        block = self.groups().get(key)
        return block if isinstance(block, dict) else {}

    def is_watched(self, group_id) -> bool:
        """是否采集这个群（托管 + 未单独关掉）。"""
        block = self.group_cfg(group_id)
        return bool(block) and bool(block.get("enabled", True))

    def defaults(self) -> dict:
        raw = self.config.get("defaults")
        block = raw if isinstance(raw, dict) else {}
        return {
            "window_hours": _positive_int(block.get("window_hours"), DEFAULT_WINDOW_HOURS),
            "push_time": _clock(block.get("push_time"), DEFAULT_PUSH_TIME),
            "max_todos": _positive_int(block.get("max_todos"), DEFAULT_MAX_TODOS),
            "mode": str(block.get("mode") or "all").strip().lower(),
            "incremental": bool(block.get("incremental", True)),
            "max_incremental_hours": _positive_int(
                block.get("max_incremental_hours"), DEFAULT_MAX_INCREMENTAL_HOURS),
        }

    def incremental(self) -> bool:
        """增量总结：只处理「上次总结之后」的新消息。

        名字不叫 `enabled` —— 框架的 `PluginBase.enabled` 是属性，别盖掉。
        """
        return self.defaults()["incremental"]

    def max_incremental_hours(self) -> int:
        return self.defaults()["max_incremental_hours"]

    def window_hours(self, group_id) -> int:
        return _positive_int(self.group_cfg(group_id).get("window_hours"),
                             self.defaults()["window_hours"])

    def push_time(self, group_id) -> str:
        """该群每日总结时刻；空串 = 不定时。"""
        return _clock(self.group_cfg(group_id).get("push_time"),
                      self.defaults()["push_time"])

    def push_on(self, group_id) -> bool:
        block = self.group_cfg(group_id)
        return bool(block) and bool(block.get("push", True)) and bool(self.push_time(group_id))

    def max_todos(self, group_id) -> int:
        return _positive_int(self.group_cfg(group_id).get("max_todos"),
                             self.defaults()["max_todos"])

    def model_for(self, group_id) -> str:
        return str(self.group_cfg(group_id).get("model") or "").strip()

    def mode_for(self, group_id) -> str:
        """该群的默认产出模式：群配置 > 全局默认。"""
        mode = str(self.group_cfg(group_id).get("mode") or "").strip().lower()
        if mode in ("all", "digest", "todo"):
            return mode
        return self.mode_default()

    def llm_cfg(self) -> dict:
        raw = self.config.get("llm")
        block = raw if isinstance(raw, dict) else {}
        return {
            "base_url": str(block.get("base_url") or "https://api.deepseek.com/v1").rstrip("/"),
            "api_key": str(block.get("api_key") or "").strip(),
            "model": str(block.get("model") or "deepseek-chat").strip(),
            "temperature": _float(block.get("temperature"), 0.2),
            "timeout": _positive_int(block.get("timeout"), 120),
            "max_context_chars": _positive_int(block.get("max_context_chars"), 14000),
        }

    def clean_cfg(self) -> dict:
        raw = self.config.get("clean")
        block = raw if isinstance(raw, dict) else {}
        noise = block.get("noise")
        return {
            "noise": [str(x) for x in noise] if isinstance(noise, (list, tuple)) else [],
            "min_len": _positive_int(block.get("min_len"), 2, minimum=0),
            "merge_seconds": _positive_int(block.get("merge_seconds"), 300, minimum=0),
            "max_msg_chars": _positive_int(block.get("max_msg_chars"), 500),
            "retention_days": _positive_int(block.get("retention_days"), DEFAULT_RETENTION_DAYS),
        }

    def media_cfg(self) -> dict:
        """图片/附件的取用上限。每一项都兜底 —— 少写一个键不该让总结挂掉。"""
        raw = self.config.get("media")
        block = raw if isinstance(raw, dict) else {}
        image = block.get("image") if isinstance(block.get("image"), dict) else {}
        file = block.get("file") if isinstance(block.get("file"), dict) else {}
        allow = file.get("allow_ext")
        return {
            "enabled": bool(block.get("enabled", True)),
            "timeout": _positive_int(block.get("timeout"), 45),
            "cache_hours": _positive_int(block.get("cache_hours"), 72, minimum=0),
            "image": {
                "enabled": bool(image.get("enabled", True)),
                "max_bytes": _positive_int(image.get("max_bytes"), 3 * 1024 * 1024),
                "max_per_run": _positive_int(image.get("max_per_run"), 8, minimum=0),
            },
            "file": {
                "enabled": bool(file.get("enabled", True)),
                "max_bytes": _positive_int(file.get("max_bytes"), 10 * 1024 * 1024),
                "max_per_run": _positive_int(file.get("max_per_run"), 3, minimum=0),
                "max_chars": _positive_int(file.get("max_chars"), 6000),
                "allow_ext": ([str(x).lower() for x in allow]
                              if isinstance(allow, (list, tuple)) else []),
            },
        }

    def mode_default(self) -> str:
        """全局默认产出模式（`all` / `digest` / `todo`）。"""
        return self.defaults().get("mode") or "all"

    def history_cfg(self) -> dict:
        raw = self.config.get("history")
        block = raw if isinstance(raw, dict) else {}
        return {
            "bootstrap_hours": _positive_int(block.get("bootstrap_hours"), 24),
            # 0 = 完全不做历史补采（只靠实时采集）。给那些
            # `get_group_msg_history` 不可用的部署留一条退路。
            "max_pages": _positive_int(block.get("max_pages"), 10, minimum=0),
            "page_size": _positive_int(block.get("page_size"), 40),
        }

    # ---- 写配置（`config.set` 会立刻落盘 config.json）

    def _group_defaults(self) -> dict:
        base = self.defaults()
        return {
            "name": "",
            "enabled": True,
            "window_hours": base["window_hours"],
            "push_time": base["push_time"],
            "push": True,
            "model": "",
            "mode": "",
            "last_push_date": "",
        }

    def update_group(self, group_id, **changes) -> dict:
        """改一个群的设置（不存在就先按默认建）。返回改完后的配置块。"""
        key = str(int(group_id))
        groups = dict(self.groups())
        block = groups.get(key)
        block = dict(block) if isinstance(block, dict) else self._group_defaults()
        block.update({k: v for k, v in changes.items() if v is not None})
        groups[key] = block
        self.config.set("groups", groups)
        return block

    def drop_group(self, group_id) -> bool:
        key = str(int(group_id))
        groups = dict(self.groups())
        if key not in groups:
            return False
        groups.pop(key)
        self.config.set("groups", groups)
        return True

    def set_default(self, key: str, value) -> bool:
        defaults = dict(self.config.get("defaults") or {})
        defaults[key] = value
        self.config.set("defaults", defaults)
        return True

    def update_llm(self, **changes) -> dict:
        """改 `llm.*`（api_key / base_url / model …），立刻落盘。

        `llm_cfg()` 每次现读 `self.config`，所以写完下一句命令就生效，不用 reload。
        """
        block = dict(self.config.get("llm") or {})
        block.update({k: v for k, v in changes.items() if v is not None})
        self.config.set("llm", block)
        return block

    # ------------------------------------------------------------------ 连接就绪

    def _register_ready_hook(self) -> None:
        """WS 就绪后补一次历史（首次安装当天不至于只有几条消息）。"""
        register = getattr(self, "on_ws_connected", None)
        if register is None:
            return
        try:
            register(self._on_connected)
        except Exception as e:                # noqa: BLE001 - 冒烟环境没有 client
            self.logger.debug("[qqnotice_sum] 注册连接回调失败：%s", e)

    async def _on_connected(self) -> None:
        from .jobs.push import bootstrap

        try:
            await bootstrap(self)
        except Exception:                     # noqa: BLE001 - 补采失败不影响正常收发
            self.logger.warning("[qqnotice_sum] 连接就绪后的补采失败", exc_info=True)


# ------------------------------------------------------------------ 小工具

def _positive_int(value, default: int, *, minimum: int = 1) -> int:
    try:
        num = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, num)


def _float(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _clock(value, default: str) -> str:
    """`"21:30"` 规范化：非法值回落默认；空串表示「不定时」，原样保留。"""
    text = str(value if value is not None else default).strip()
    if not text or text in ("关", "off", "none", "关闭"):
        return ""
    if len(text) == 4 and text[1] == ":":
        text = "0" + text
    try:
        hour, minute = text.split(":")
        hour, minute = int(hour), int(minute)
    except (ValueError, AttributeError):
        return default
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return default
    return f"{hour:02d}:{minute:02d}"


__all__ = [
    "QQNoticeSumPlugin", "COMMAND_NAME",
    "DEFAULT_WINDOW_HOURS", "DEFAULT_PUSH_TIME", "DEFAULT_MAX_TODOS",
    "DEFAULT_MAX_INCREMENTAL_HOURS",
]
