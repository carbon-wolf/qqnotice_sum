# qqnotice_sum

读指定 QQ 群的消息，用大模型整理成**群内容摘要 + 待办清单**，也能**直接提问**。

待办只是其中一块：它同样会把这段时间群里发生了什么、发了什么通知、聊出了什么结论
梳理成几段话 —— 包括**图片和文件**里的内容。想只问一句「谁负责收表」也行，
不用等日报，私聊一句话就有答案，还带原话出处。

[ncqqpy](https://github.com/Green1423/ncqqpy) 插件，独立仓库形态：框架按
`plugin_dir`（默认 `./plugins`）扫子目录加载，clone 进去就能用。

## 安装

```bash
cd /path/to/ncqqpy
git clone https://github.com/carbon-wolf/qqnotice_sum.git plugins/qqnotice_sum
# QQ 私聊机器人（管理员）
/reload qqnotice_sum
```

依赖：框架自带 aiohttp，**必装项为零**。想解析 PDF / Word / Excel 再按需装：

```bash
pip install pypdf python-docx openpyxl    # 全可选，不装就只解析纯文本类
```

## 命令只在私聊

`/sum` 声明为 `scope="private"`：群里 @机器人 发 `/sum` **不会响应**（也不会在群里刷屏）。
每日总结由定时任务直接推到群里，不走命令。

只注册 `sum` 一条命令（别名 `待办`、`td`），所有动作都是它后面的参数 ——
不占七八个顶层命令名，帮助也只有一页。

```
/sum summary [24h] [mode]   摘要 + 待办（默认；mode = all / digest / todo）
/sum digest  [24h]          只看群内容摘要
/sum todo    [24h]          只看待办（跳过图片附件，快且省 token）
/sum list    [all]          待办清单：不带群名就列出**所有**托管群（all = 连已完成）
/sum done    <编号>         标记完成（编号全局唯一，不用报群号）
/sum drop    <编号>         忽略（以后不再重复提醒）
/sum sync    [24h]          强制从群历史补采一次
/sum ask     <问题>         直接提问（谁负责 / 几点 / 在哪 / 要交什么）
/sum config  …              群配置 + 模型配置（见下）
/sum status                 采集与模型状态
```

中文别名同样可用：`总结` / `摘要` / `待办` / `列表` / `完` / `弃` / `补` / `设置` / `状态`。
窗口写法：`24h` `12h` `90m` `3d` `1d12h` `today` `yesterday` `week` `all`。

**群号可以省略**：只托管一个群时直接 `/sum summary`。

托管群不止一个时，用**群名**比记号码省事得多：

```text
/sum config add 987654321        ← 加的时候会自动把群名拉下来
/sum summary 计院 24h            ← 名字里的一段就行
/sum summary 计院26级通知群 24h   ← 全名当然也行
/sum summary 987654321 24h       ← 群号仍然可用
```

认不出或者撞名了，会直接把已托管的群列出来（带名字和群号），照着说一遍就行。
名字想改就 `/sum config name 计院 26级通知（改）`；接口查不到名字时也可以手动设。

### 输出长什么样

```
📋 计算机学院通知群（987654321） · 11-03 15:00 ~ 11-04 15:00
消息 128 条（去噪合并后 67 条） · 图 4 · 文件 1 · deepseek-chat

【概要】
学院周五下午在 302 开全体会，要求全员到场；宿舍楼周六全天停水；
开题报告下周一前交到教务系统。群里还转发了一份课程安排表。

【要点】
1. 周五 15:00 · 302 开会，全员必须到
2. 周六全天停水
3. 开题报告下周一前提交到教务系统

【待办】
1. [高]参加 302 全体会
   截止 2026-11-06 15:00
2. 提交开题报告
   截止 2026-11-09 23:59
```

### 待办清单

`/sum list` **不指定群就列出全部托管群** —— 查待办问的是「我还有哪些事」，
不是「某个群有哪些事」，不该先拦一道问群号：

```
📋 待办清单 · 3 个群

计院26级通知群（987654321）
  #1 [高]交开题报告
     截止 2026-11-06 17:00
  #2 参加 302 全体会
     @班长 · 截止 2026-11-06 15:00

校团委志工部（987654322）
  #3 报志愿者名单
     @小李 · 截止 2026-11-08 18:00

没有未完成待办：篮球队（987654323）

共 3 条未完成。编号全局唯一，标记时不用报群号：
/sum done <编号> 完成　/sum drop <编号> 忽略
```

待办的 `#编号` 是全局唯一的，所以 `/sum done 3` 就够，插件会自己认出 #3 属于哪个群。
要只看某个群就照常说群名：`/sum list 计院`。看历史处理过的加 `all`。

## 提问

总结是「我告诉你发生了什么」，提问是「我想知道某一件事」。后者更常用 ——
群里 200 条消息翻起来太累，直接问就行：

```
/sum ask 这周有什么要交的
/sum ask 谁负责收表
/sum ask 开会定在几点
/sum ask 上次发的课表在哪
```

**连 `ask` 都不用写**，第一个词认不出来就当问题处理：

```
/sum 谁负责收表
```

回答长这样：

```
💬 谁负责收表
范围 11-03 15:00 ~ 11-04 15:30 · 128 条消息（筛出 12 条相关的）

学习委员收齐后交给班长，截止周三 18:00。

依据
  2）学习委员 11-04 09:12：各班把表填好发我，我汇总
  5）班长 11-04 09:30：周三 18:00 前我要拿到汇总表
```

几件事值得说清楚：

* **只读**。提问不会产生待办、不改任何状态，问一百次也不会污染库。
* **带依据**。回答里的编号能对回具体那条消息（谁、几点、原话），
  不是凭空生成的一段话 —— 群通知这种事，能溯源比说得漂亮重要。
* **时间词自动识别**。`这周` / `昨天` / `最近3天` / `全部` 写在问题里就行，
  不用另外给参数；也可以放前面：`/sum ask 7d 上次的课表`。
* **只看文字消息**。图片和附件在提问时**不解析**（总结时会）——
  逐张下载解码太慢，问答要的是秒回。问截图内容会答「记录里没提到」。
* 记录不够时会直说「记录里没提到」，并提示放宽窗口重问，**不会编**。

检索不用 jieba：中文按**字符二元组**算重叠（「开题」「题报」「报告」），
几十 MB 的分词依赖换不来什么精度，还怕新词。

更关键的是**不用均匀抽稀**。总结那种「按时间均匀抽样」保留的是时间分布形态，
适合摘要；问答要的是「包含答案的那几条」，均匀抽稀正好会把它们抽掉。
所以这里按相关度筛：命中的二元组越多越靠前，长消息做长度惩罚（防一篇长文霸榜），
问题里点了某人名会给他的发言加权。同分时**新的优先** —— 预算不够时，
刚发的通知比三天前的闲聊重要。

## 身份

「待办」这两个字对不同人是不同的东西。学委看到的是「要交的作业」，
班长看到的是「要通知到的人」，干事看到的是「要报的材料」。

所以可以给总结设一个身份：

```text
/sum config persona 26级学委
/sum config persona 班级心理委员，只关心统计和上报
/sum config persona                       ← 看当前设成什么
/sum config persona clear                 ← 清空
/sum config persona 987654321 班长        ← 只给某个群设
```

它**主要影响筛什么，不只是措辞**。设成「26级学委」之后，提示词里会多出这一段：

```text
【你的身份】26级学委

先想清楚这个身份在群里关心什么，再按这个视角处理：
1. 只把**这个身份需要跟进或知道**的事列进待办；与它无关的
   （别的年级、别的部门、纯通知性质）不要列，除非明确要求全员参与；
2. 优先级按这个身份的紧迫感判断 —— 自己要被催、有截止日期的排前面；
3. 概要按这个身份的口吻写，像同班同学直接告诉你，别写成公文。
```

没设身份时这一段不会出现，prompt 跟以前一模一样 —— 不想用就别设，不影响。

两条规则：

* **群级 > 全局**。同一个号可能在两个群里是两个身份（在班级群是学委，
  在社团群是普通成员），所以群级设置会覆盖全局。
* **提问也跟着换视角**。`ask` 用的是「提问者的身份」，需要你去做的事会直接说
  「你要…」。

## 增量总结

**默认只处理「上次总结之后」的新消息**，而不是每次都回溯固定 24 小时。

```
你说「/sum summary」         → 从未总结过：回溯 window_hours（默认 24h）
再「/sum summary」           → 📭 上次总结（15:48）之后没有新消息
来了一条新的，再「/sum summary」→ 只处理那一条
```

这么做的理由很直接：昨天已经总结过的内容再喂一遍，既费 token，也会让
「今天有什么事」被昨天的旧消息冲淡。定时推送同理 —— 晚上 9 点半推的
就是**当天新增**的那一段。

几条边界：

| 情况 | 行为 |
|---|---|
| 从没总结过 | 回溯 `window_hours` |
| 上次总结到现在跨度 > 7 天 | 截断到 7 天（`max_incremental_hours`），标「跨度已截到上限」 |
| 刚总结完又要总结 | 直接说「没有新消息」，**不调模型** |
| 手动给了窗口 | 完全不掺增量，按你给的来 |
| 提问（ask） | **不用增量**，问的是一段时间就给完整一段 |

想看完整一段就显式给窗口：`/sum summary 24h`。想彻底关掉：

```
/sum config incremental off
```

「上次总结」以**成功**那次为准：模型报错、返回没结构化时不算，下次会重试
（游标不推进，不会静默丢消息）。

## 图片与附件

通知群的干货经常在截图和附件里，所以：

**取字节走框架的接口**，不是自己 HTTP 拉。按文档的三层入口，能探测到哪层用哪层：

```python
self.files.download_image_stream(file=..., file_id=...)   # 图片（§11.1 标准入口）
self.files.download_stream(url, file_id=...)              # 文件
self.api.download_file_image_stream(...)                  # 低层，等价（§11.3）
self.api.download_file(url=...)                           # 更老的框架只有这个
```

这是让 NapCat 那边去下载再传回来 —— 它就在 QQ 环境里，能拿到我们拿不到的东西。
自己拉 URL 基本行不通：图片链带防盗链（403）、群文件链要 cookie（401）、
消息段里的 `file` 常常是 `file://` 本地路径。

三级降级，谁都可能踩空，所以都留着：

| 顺序 | 方式 | 说明 |
|---|---|---|
| 1 | 流式（`self.files` → `self.api`） | 不落盘、超限能立刻掐断 |
| 2 | `api.download_file(url=...)` | 老框架也有；下到 NapCat 那台机器后读回来 |
| 3 | 直连 URL | 公网 CDN 图片偶尔能成 |

群文件会先用 `get_group_file_url` 换成带鉴权的直链再交给上面三层。
流式读的途中就计数，超限立刻 `aclose()`，不会先把 200MB 读进内存再判断。

* **图片**以 multimodal 方式直接发给模型（`image_url` + base64），聊天记录里会标成
  `[图1]`、`[图2]`，与附带的图片顺序一一对应；
* **文件**下载后按扩展名提取文本，拼成「附件内容」一节交给模型：

| 扩展名 | 处理 | 需要 |
|---|---|---|
| `.txt .md .csv .json .log .yaml .xml .html` 等 | 直接解码（utf-8 / gb18030） | 无 |
| `.pdf` | 取前 20 页文本 | `pypdf`（或 `PyPDF2`） |
| `.docx` | 段落 + 表格 | `python-docx` |
| `.xlsx` | 前 3 个 sheet、前 50 行 | `openpyxl` |
| 其它 | 只告诉模型「有个叫 xxx 的文件」 | — |

**限制都是硬的**（`config.json → plugins.qqnotice_sum.media`）：

| 项 | 默认 | 说明 |
|---|---|---|
| `image.max_bytes` | 3 MB | 单张超过就跳过，不下载 |
| `image.max_per_run` | 8 | 每次总结最多看几张 |
| `file.max_bytes` | 10 MB | 单个超过就不下载（先看 `Content-Length`，超了连流都不开） |
| `file.max_per_run` | 3 | 每次总结最多解析几个 |
| `file.max_chars` | 6000 | 单个文件提取多少字 |
| `file.allow_ext` | 见上表 | 白名单，不在列表直接跳过 |
| `cache_hours` | 72 | 同一个文件 3 天内只下载解析一次 |

被跳过的附件会在报告末尾用一行说明（`（附件跳过：文件2（12.3MB，超过文件上限））`），
不会静默消失。

**模型不支持图片怎么办**：如果接口对带图的请求回 400，会自动去掉图片重试一次
（日志里有一行 warning），总结不会因此失败。

## 权限

权限**全部交给框架**，插件内部不自己判等级：

```python
@command("sum", permission="admin", scope="private", aliases=["待办", "td"], ...)
```

* `scope="private"` —— 群里不响应；
* `permission="admin"` —— 私聊里只有超管和 `/userperm` 提级过的人够得着。

运行时按框架的标准手段调整，不用改代码：

```text
/perm set sum member            放开给所有人（私聊）
/perm reset sum                 恢复成代码里的 admin
/userperm set 123456 admin      单独给某个人提级
```

## 配置

构建插件时会把全部配置项写进 `config.json → plugins.qqnotice_sum`（只补缺、不覆盖）。

**但日常不用去改那个文件**：模型密钥和接口地址可以直接私聊配。

```text
/sum config key sk-xxxxxxxxxxxx              配密钥（发完自动撤回这条消息）
/sum config key sk-xxxx https://.../v1       一条命令同时配密钥和接口
/sum config key                              看当前密钥（脱敏成 sk-a…cdef）
/sum config key clear                        清空
/sum config api https://api.deepseek.com/v1  换接口
/sum config api                              看当前接口 + 常见几家写法
/sum config api clear                        恢复默认（DeepSeek）
/sum config test                             真发一次请求，确认能调通
```

几个约束，都是刻意加的：

* **只有超管能改**。这一项影响所有群和账单，不该跟「某群开关」一个待遇。
  想放权：`/userperm set <QQ> superuser`。
* **只有私聊能发**（`scope="private"`），且回复里**永远不回显完整密钥**，只显示 `sk-a…cdef（25 位）`。
* 保存后**立刻生效**，不用 `/reload`。
* 密钥明文落在 `config.json` 里 —— 这是框架的配置机制决定的，注意那个文件的权限
  （别提交到 git，`.gitignore` 已经排除了 `config.json`）。
* `/sum config test` 是配完必做的一步：接口填错最常见的结果是「存住了但一次都调不通」。
  它会把 HTTP 状态、接口地址、模型名一起回给你。

手动改 `config.json` 也行（适合批量部署），字段如下：

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
  "defaults": {
    "window_hours": 24,
    "push_time": "21:30",
    "max_todos": 30,
    "mode": "all",
    "persona": "",
    "incremental": true,
    "max_incremental_hours": 168
  },
  "groups": {
    "987654321": {
      "name": "计算机学院通知群",
      "persona": "26级学委",
      "enabled": true,
      "window_hours": 24,
      "push_time": "21:30",
      "push": true,
      "model": "",
      "mode": "all",
      "last_push_date": ""
    }
  },
  "media": {
    "enabled": true,
    "timeout": 45,
    "cache_hours": 72,
    "image": { "enabled": true, "max_bytes": 3145728, "max_per_run": 8 },
    "file": {
      "enabled": true,
      "max_bytes": 10485760,
      "max_per_run": 3,
      "max_chars": 6000,
      "allow_ext": [".txt", ".md", ".csv", ".json", ".pdf", ".docx", ".xlsx"]
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
  换家只改 `base_url` + `model`。要用图片就得选支持视觉的模型。
* `api_key` 不填时总结会直接报「配置错误」，不会静默失败。用命令配更快：
  `/sum config key sk-xxx`（见上一节）。
* `clean.noise` 是整条丢弃的无信息量短句；`merge_seconds` 控制同一人连发多久内合并
  （合并时**附件会跟着并过来**，不会因为「先发文字后发图」而丢图）。
* `max_context_chars` 是给模型的聊天记录上限，超了按时间均匀抽样。

## 数据

**用框架提供的 sqlite ORM，插件不自己开连接、不另建库**：

```python
self.database(ensure=[Msg, Todo, Run, SyncState, Media])   # napcat.store.Database
Msg.objects.filter(group_id=..., ts__gte=...)              # napcat.store.Model
```

库文件由框架决定：`plugins/qqnotice_sum/data/qqnotice_sum.db`，卸载/热重载时框架统一
`close()`。`/sum status` 会把它打印出来。加字段走 `ensure_schema` 的 `ALTER TABLE`，
老库升级不用手工迁移。

| 表 | 内容 |
|---|---|
| `qs_msg` | 原始群消息（`message_id` 唯一；`media` 存图片/文件的坐标，采集时不下载） |
| `qs_todo` | 待办（`fp` 内容指纹去重，同一事项跨多次总结不会重复计数） |
| `qs_run` | 每次总结的记录（窗口 / 条数 / 模型 / 耗时 / 摘要 / 要点） |
| `qs_sync` | 每群的采集游标 |
| `qs_media` | 附件解析缓存（同一个文件只下载解析一次） |

超过 `clean.retention_days` 的消息与附件缓存自动清理；待办保留（它是结论，不是原料）。

## 排错

**总结说「这段时间没采集到消息」**
看 `/sum status` 的「上次同步」。多数是框架监听范围：`config.json` 里 `bound_groups`
非空时**只有列出的群**会被分发消息。要么把通知群加进去，要么让 `bound_groups` 保持空、
`all_groups: true`。也可能是机器人掉线过 —— 发 `/sum sync` 从群历史捞一次。

如果是「上次总结（xx）之后没有新消息」，那是**增量在正常工作**，不是故障；
想重看整段就 `/sum summary 24h`。

**图片没被读懂**
先确认用的是支持视觉的模型；再看日志有没有「模型不收图片，去掉图片重试」——
有的话说明端点不支持 `image_url`，换模型或把 `media.image.enabled` 关掉。
另外单张超过 `image.max_bytes` 的会被直接跳过。

**图片 / 文件取不到**
报告末尾会写清是哪几级失败了，例如：

```
文件1（流式：当前框架版本没有流式下载接口；download_file：没给回内容；
      直连：直连被拒（HTTP 403，多半是防盗链））
```

按那条对：
- 只有「流式」失败 → 走的是老框架，第 2 级应该能接上；两级都失败说明
  框架与 NapCat **不在同一台机器**且没有流式接口 —— 这种部署确实取不到附件。
- 全部失败 → 先看 `media.image.max_bytes` / `file.max_bytes` 是不是太小，
  再确认框架与 NapCat 的连接没断（附件是让 NapCat 去取的）。
- `media.enabled` 被关掉时不会有任何提示，直接跳过。

**补采失败：`[1200] 消息undefined不存在`**
有些 NapCat 版本不认「省略 `message_seq`」这种传法，会拿 `undefined` 去查消息。
1.6.0 起会自动重试（改传 `message_seq=0`），不用管。如果两种都失败，
错误里会写「不传 message_seq 与传 0 都试过」。
实在用不了就把 `history.max_pages` 设成 `0`：跳过历史补采，只靠实时采集
（从装插件那一刻起积累）。

**`config add` 之后没显示群名**
`get_group_info` 没返回（权限不足、连接问题，或者这个群拿不到信息）。
不影响功能，群号照样能用；想按名字选群就手动起一个：
`/sum config name 计院通知群`。

**PDF / Word 解析不了**
报告里会写「格式需要额外依赖才能解析」。`pip install pypdf python-docx openpyxl` 即可。

**报「配置错误：没配 llm.api_key」**
私聊发 `/sum config key sk-xxxx`（超管），然后 `/sum config test` 验一下。
手动改也行：填 `config.json` 的 `plugins.qqnotice_sum.llm.api_key`，`/reload qqnotice_sum`。

**群里发 `/sum` 没反应**
正常 —— 本插件只认私聊。推送由定时任务完成，不需要在群里发命令。

**总结很慢**
补采走 NapCat 历史接口（每次调用 0.5~1s 串行节流），命令里最多翻 3 页、定时任务 2 页；
附件下载与解析也占时间。可以调小 `max_per_run` / `max_chars` / `max_context_chars`。

## 开发 / 自测

```bash
cd /path/to/ncqqpy
./venv/Scripts/python.exe smoke_load.py                                   # 加载冒烟
./venv/Scripts/python.exe -B plugins/qqnotice_sum/tests/test_qqnotice_sum.py  # 238 项用例
```

用例不连 QQ、不连网：端到端部分是真实插件实例 + 框架 sqlite，模型调用用假函数替换。

## 文件结构

| 文件 | 职责 |
|---|---|
| `plugin.py` | 只有装配：元信息、配置模板、生命周期 |
| `commands/main.py` | 唯一一条 `/sum` 的动作分发 |
| `events/message.py` | 实时采集落库（含附件坐标） |
| `jobs/push.py` | 到点推送 / 停机补漏 / 历史清理 |
| `service.py` | 总结 / 提问链路（命令与定时任务共用，保证去重口径一致） |
| `media.py` | 附件取字节（走框架流式下载，非直连）+ 解析 + 编号标注 |
| `collect.py` | 消息段 → 文本 + 附件描述 |
| `history.py` | 历史消息分页补采 |
| `digest.py` | 清洗 / 合并 / 预算裁剪 / 提示词 |
| `qa.py` | 提问：按问题筛消息（二元组打分）+ 回答 JSON 容错解析 |
| `llm.py` | OpenAI 兼容调用（含带图降级）+ 报告 JSON 容错解析 |
| `render.py` | 面板 / 列表 / 摘要 / 待办 / 回答的文案 |
| `store.py` | 五张表的模型（基于 `napcat.store`） |
| `schemas.py` | 参数 Schema、动作 / 模式 / 窗口解析 |

## License

MIT
