# qqnotice_sum

通知群消息 → 待办清单。读指定 QQ 群的消息，用大模型总结成**可执行的待办**，
私聊随时查询，每天定时推送到群。

[ncqqpy](https://github.com/Green1423/ncqqpy) 插件，独立仓库形态：框架按
`plugin_dir`（默认 `./plugins`）扫子目录加载，clone 进去就能用。

## 安装

```bash
cd /path/to/ncqqpy
git clone https://github.com/carbon-wolf/qqnotice_sum.git plugins/qqnotice_sum
# QQ 私聊机器人（管理员）
/reload qqnotice_sum
```

依赖：只要框架能 import `napcat`（aiohttp 框架自带），无需额外装包。

## 命令只在私聊

`/sum` 声明为 `scope="private"`：群里 @机器人 发 `/sum` **不会响应**（也不会在群里刷屏）。
每日总结由定时任务直接推到群里，不走命令。

只注册 `sum` 一条命令（别名 `待办`、`td`），所有动作都是它后面的参数 ——
不占七八个顶层命令名，帮助也只有一页。

```
/sum                        概览面板
/sum summary [24h]          立即总结一次
/sum list [all]             未完成的待办（all = 连已完成一起列）
/sum done <编号>            标记完成
/sum drop <编号>            忽略（以后不再重复提醒）
/sum sync [24h]             强制从群历史补采一次
/sum config …               群配置（见下）
/sum status                 采集与模型状态
```

中文别名同样可用：`总结` / `列表` / `完` / `弃` / `补` / `设置` / `状态`。

窗口写法：`24h` `12h` `90m` `3d` `1d12h` `today` `yesterday` `week` `all`
（中文：今天 / 昨天 / 本周 / 全部）。

**群号可以省略**：只托管一个群时直接 `/sum summary`；托管多个群时用
`/sum summary 987654321 24h` 指定（多个群不指定会提示已托管列表）。

### 配置

| 写法 | 作用 |
|---|---|
| `/sum config add <群号>` | 托管一个群（开始采集 + 可推送） |
| `/sum config del <群号>` | 取消托管（已采数据和待办保留） |
| `/sum config window 24h` | 总结默认看多久 |
| `/sum config time 21:30` | 每天几点自动推送（`off` = 不定时） |
| `/sum config push on\|off` | 该群是否自动推送 |
| `/sum config model <名字>` | 该群用哪个模型（`default` = 跟随全局） |
| `/sum config switch on\|off` | 该群是否继续采集 |
| `/sum config default window\|time\|max <值>` | 改全局默认（仅超管） |
| `/sum config` | 配置总览 |

群号写在项前后都认：`/sum config window 987654321 12h`。

## 权限

权限**全部交给框架**，插件内部不自己判等级：

```python
@command("sum", permission="admin", scope="private", aliases=["待办", "td"], ...)
```

* `scope="private"` —— 群里不响应；
* `permission="admin"` —— 私聊上下文里 `admin(60)` 只有超管和 `/userperm` 提级过的人够得着。

运行时可以按框架的标准手段调整，不用改代码：

```text
/perm set sum member            放开给所有人（私聊）
/perm reset sum                 恢复成代码里的 admin
/userperm set 123456 admin      单独给某个人提级
/help sum                       看到的权限标注就是生效值
```

## 配置

构建插件时会把全部配置项写进 `config.json → plugins.qqnotice_sum`（只补缺、不覆盖）。

```json
{
  "enabled": true,
  "llm": {
    "base_url": "https://api.deepseek.com/v1",
    "api_key": "",
    "model": "deepseek-chat",
    "temperature": 0.2,
    "timeout": 120,
    "max_context_chars": 14000
  },
  "defaults": { "window_hours": 24, "push_time": "21:30", "max_todos": 30 },
  "groups": {
    "987654321": {
      "name": "",
      "enabled": true,
      "window_hours": 24,
      "push_time": "21:30",
      "push": true,
      "model": "",
      "last_push_date": ""
    }
  },
  "clean": {
    "noise": ["收到", "好的", "谢谢", "1", "ok", "嗯嗯", "打卡", "已阅"],
    "min_len": 2,
    "merge_seconds": 300,
    "max_msg_chars": 500,
    "retention_days": 60
  },
  "history": { "bootstrap_hours": 24, "max_pages": 10, "page_size": 40 }
}
```

* `llm.base_url` 用任意 OpenAI 兼容口（DeepSeek / 通义 / 自建 vLLM / Ollama），
  换家只改 `base_url` + `model`。
* `api_key` 不填时总结会直接报「配置错误」，不会静默失败。
* `groups` 的键就是要采集/推送的群，没有第二份白名单。

## 数据

**用框架提供的 sqlite ORM，插件不自己开连接、不另建库**：

```python
self.database(ensure=[Msg, Todo, Run, SyncState])   # napcat.store.Database
Msg.objects.filter(group_id=..., ts__gte=...)        # napcat.store.Model
```

库文件由框架决定：`plugins/qqnotice_sum/data/qqnotice_sum.db`，卸载/热重载时框架统一
`close()`。`/sum status` 会把它打印出来。

| 表 | 内容 |
|---|---|
| `qs_msg` | 原始群消息（`message_id` 唯一，实时采集与历史补采天然去重） |
| `qs_todo` | 待办（`fp` 内容指纹去重，同一个事项跨多次总结不会重复计数） |
| `qs_run` | 每次总结的运行记录（窗口 / 条数 / 模型 / 耗时） |
| `qs_sync` | 每群的采集游标 |

超过 `clean.retention_days` 的消息自动清理；待办保留（它是结论，不是原料）。

## 排错

**总结说「这段时间没采集到消息」**
看 `/sum status` 的「上次同步」。多数是框架监听范围：`config.json` 里 `bound_groups`
非空时**只有列出的群**会被分发消息。要么把通知群加进去，要么让 `bound_groups` 保持空、
`all_groups: true`。也可能是机器人掉线过 —— 发 `/sum sync` 从群历史捞一次。

**报「配置错误：没配 llm.api_key」**
填 `config.json` 的 `plugins.qqnotice_sum.llm.api_key`，然后 `/reload qqnotice_sum`。

**群里发 `/sum` 没反应**
正常 —— 本插件只认私聊。推送由定时任务完成，不需要在群里发命令。

**私聊发 `/sum` 没反应**
`permission=admin`：非管理员看到的是「未知命令」。用 `/userperm set <QQ> admin`
或在私聊里让超管执行 `/perm set sum member`。

**总结很慢**
补采走 NapCat 历史接口，每次调用有 0.5~1s 串行节流。命令里最多翻 3 页、定时任务
最多 2 页。群特别活跃时把 `llm.max_context_chars` 与 `defaults.max_todos` 调小一点。

## 开发 / 自测

```bash
cd /path/to/ncqqpy
./venv/Scripts/python.exe smoke_load.py                                     # 加载冒烟
./venv/Scripts/python.exe -B plugins/qqnotice_sum/tests/test_qqnotice_sum.py  # 78 项用例
```

用例不连 QQ、不连网：端到端部分是真实插件实例 + 框架 sqlite，模型调用用假函数替换。

## 文件结构

| 文件 | 职责 |
|---|---|
| `plugin.py` | 只有装配：元信息、配置模板、生命周期 |
| `commands/main.py` | 唯一一条 `/sum` 的动作分发 |
| `events/message.py` | 实时采集落库 |
| `jobs/push.py` | 到点推送 / 停机补漏 / 历史清理 |
| `service.py` | 总结链路（命令与定时任务共用，保证去重口径一致） |
| `collect.py` | 消息段 → 文本（含 JSON 卡片、文件、@） |
| `history.py` | 历史消息分页补采 |
| `digest.py` | 清洗 / 合并 / 预算裁剪 / 提示词 |
| `llm.py` | OpenAI 兼容调用 + 待办 JSON 容错解析 |
| `render.py` | 面板 / 列表 / 总结的文案 |
| `store.py` | 四张表的模型（基于 `napcat.store`） |
| `schemas.py` | 参数 Schema、动作与窗口解析 |

## License

MIT
