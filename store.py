# -*- coding: utf-8 -*-
"""qqnotice_sum 的数据模型与查询帮手。

四张表：

    qs_msg     原始群消息（采集来的，清洗前的原文）
    qs_todo    待办条目（带指纹去重与状态）
    qs_run     每次总结的运行记录（哪段时间、用了多少条、产出几条）
    qs_sync    每个群的采集游标（停机后从哪儿接着补）

`Model` 来自 `napcat.store`（Django 风格），库文件由框架管：
`plugins/qqnotice_sum/data/qqnotice_sum.db`，卸载/热重载时框架自动 close。
"""
from __future__ import annotations

import hashlib
import re
import time
from typing import Iterable, Optional

from napcat.store import AutoField, CharField, IntegerField, Model, TextField


class Msg(Model):
    """一条群消息。`message_id` 唯一 —— 实时采集与历史补采会撞上同一批消息。"""

    class Meta:
        table = "qs_msg"

    id = AutoField()
    group_id = IntegerField(index=True)
    message_id = IntegerField(unique=True, index=True)
    seq = IntegerField(default=0)
    user_id = IntegerField(default=0)
    sender = CharField(max_length=64, default="")
    text = TextField(default="")
    kind = CharField(max_length=16, default="text")
    ts = IntegerField(index=True, default=0)


class Todo(Model):
    """一条待办。`fp` 是内容指纹，用来跨多次总结去重。"""

    class Meta:
        table = "qs_todo"

    id = AutoField()
    group_id = IntegerField(index=True)
    fp = CharField(max_length=32, unique=True, index=True)
    title = CharField(max_length=200)
    owner = CharField(max_length=64, default="")
    due = CharField(max_length=32, default="")
    priority = CharField(max_length=8, default="normal")
    status = CharField(max_length=8, default="open", index=True)   # open/done/dropped
    evidence = TextField(default="")
    run_id = IntegerField(default=0)
    created_at = IntegerField(index=True, default=0)
    done_at = IntegerField(default=0)
    done_by = IntegerField(default=0)


class Run(Model):
    """一次总结的运行记录（面板与排错都靠它）。"""

    class Meta:
        table = "qs_run"

    id = AutoField()
    group_id = IntegerField(index=True)
    start_ts = IntegerField(default=0)
    end_ts = IntegerField(default=0)
    msg_count = IntegerField(default=0)
    todo_new = IntegerField(default=0)
    todo_open = IntegerField(default=0)
    model = CharField(max_length=64, default="")
    cost_ms = IntegerField(default=0)
    created_at = IntegerField(index=True, default=0)


class SyncState(Model):
    """每个群的采集游标。`last_seq` 用于历史翻页，`last_ts` 用于判断要不要补漏。"""

    class Meta:
        table = "qs_sync"

    id = AutoField()
    group_id = IntegerField(unique=True, index=True)
    last_ts = IntegerField(default=0)
    last_seq = IntegerField(default=0)
    last_sync = IntegerField(default=0)


# ------------------------------------------------------------------ 指纹

_PUNCT_RE = re.compile(r"[\s\u3000,，。.;；:：!！?？、'\"“”‘’()（）\[\]【】<>《》\-—_/\\|·~`*#]+")


def normalize_title(title: str) -> str:
    """去掉空白与标点、统一大小写 —— 让「提交报告」和「提交 报告。」算同一条。"""
    text = str(title or "").strip().lower()
    text = _PUNCT_RE.sub("", text)
    return text[:120]


def fingerprint(group_id, title: str, owner: str = "") -> str:
    """内容指纹：群 + 归一化标题 + 责任人。32 位十六进制。"""
    raw = f"{int(group_id)}|{normalize_title(title)}|{str(owner or '').strip().lower()}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


# ------------------------------------------------------------------ 消息

def existing_message_ids(message_ids: Iterable[int]) -> set[int]:
    """这批 message_id 里哪些已经在库里（分批查，避免 SQL 变量过多）。"""
    ids = [int(x) for x in message_ids if x]
    found: set[int] = set()
    for i in range(0, len(ids), 400):
        chunk = ids[i:i + 400]
        for row in Msg.objects.filter(message_id__in=chunk):
            found.add(int(row.message_id))
    return found


_SQL_INT_MAX = (1 << 62) - 1


def _as_int(value, default: int = 0) -> int:
    """转整数并夹到 sqlite INTEGER 范围内（`message_seq` 偶尔是超长数字串）。"""
    try:
        num = int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return default
    if num > _SQL_INT_MAX or num < -_SQL_INT_MAX:
        return default
    return num


def save_messages(db, group_id, rows: Iterable[dict]) -> int:
    """批量入库（去重后）。`rows` 每项：message_id/seq/user_id/sender/text/kind/ts。"""
    gid = int(group_id)
    batch, seen = [], set()
    for row in rows:
        mid = _as_int(row.get("message_id"))
        if mid <= 0 or mid in seen:
            continue
        seen.add(mid)
        batch.append((mid, row))
    if not batch:
        return 0
    exists = existing_message_ids([mid for mid, _ in batch])
    pending = [(mid, row) for mid, row in batch if mid not in exists]
    if not pending:
        return 0
    with db.atomic():
        for mid, row in pending:
            Msg.objects.create(
                group_id=gid,
                message_id=mid,
                seq=_as_int(row.get("seq")),
                user_id=_as_int(row.get("user_id")),
                sender=str(row.get("sender") or "")[:64],
                text=str(row.get("text") or ""),
                kind=str(row.get("kind") or "text")[:16],
                ts=_as_int(row.get("ts")),
            )
    return len(pending)


def messages_between(group_id, start_ts: int, end_ts: int, *, limit: int = 0) -> list[dict]:
    """取某个群在 `[start_ts, end_ts]` 内的消息，按时间升序。"""
    qs = Msg.objects.filter(group_id=int(group_id),
                            ts__gte=int(start_ts),
                            ts__lte=int(end_ts)).order_by("ts")
    if limit and limit > 0:
        qs = qs.limit(int(limit))
    return [row.to_dict() for row in qs]


def message_count(group_id, *, since_ts: int = 0) -> int:
    qs = Msg.objects.filter(group_id=int(group_id))
    if since_ts:
        qs = qs.filter(ts__gte=int(since_ts))
    return qs.count()


def latest_message_ts(group_id) -> int:
    row = Msg.objects.filter(group_id=int(group_id)).order_by("-ts").first()
    return int(row.ts) if row is not None else 0


def purge_messages(db, *, before_ts: int) -> int:
    """删掉早于 `before_ts` 的消息（保留待办本身，待办是结论不是原料）。"""
    with db.atomic():
        return Msg.objects.filter(ts__lt=int(before_ts)).delete()


# ------------------------------------------------------------------ 采集游标

def sync_state(group_id) -> Optional[SyncState]:
    return SyncState.objects.filter(group_id=int(group_id)).first()


def touch_sync(group_id, *, last_ts: int = 0, last_seq: int = 0) -> SyncState:
    """更新游标（没有就建）。`0` 表示「这一项不动」。"""
    gid = int(group_id)
    row = sync_state(gid)
    if row is None:
        return SyncState.objects.create(group_id=gid, last_ts=int(last_ts),
                                        last_seq=int(last_seq), last_sync=int(time.time()))
    if last_ts and int(last_ts) > int(row.last_ts or 0):
        row.last_ts = int(last_ts)
    if last_seq:
        row.last_seq = int(last_seq)
    row.last_sync = int(time.time())
    row.save()
    return row


# ------------------------------------------------------------------ 待办

def open_todos(group_id, *, limit: int = 0) -> list[dict]:
    qs = Todo.objects.filter(group_id=int(group_id), status="open").order_by("-created_at")
    if limit and limit > 0:
        qs = qs.limit(int(limit))
    return [row.to_dict() for row in qs]


def get_todo(todo_id: int) -> Optional[Todo]:
    return Todo.objects.filter(id=int(todo_id)).first()


def upsert_todos(group_id, items: Iterable[dict], *, run_id: int = 0,
                 now: Optional[int] = None) -> tuple[int, int]:
    """把模型产出的待办写进库。

    * 同指纹且状态 `open` → 刷新截止/优先级/依据，不重复计数；
    * 同指纹但已 `dropped` → 用户明确忽略过，**不复活**；
    * 同指纹但已 `done` → 保持完成，也不复活（重新提起大概率是同一件事）。

    Returns:
        `(新增条数, 当前 open 总数)`
    """
    gid = int(group_id)
    stamp = int(now if now is not None else time.time())
    created = 0
    for item in items:
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        owner = str(item.get("owner") or "").strip()[:64]
        due = str(item.get("due") or "").strip()[:32]
        priority = str(item.get("priority") or "normal").strip().lower()
        if priority not in ("high", "normal", "low"):
            priority = "normal"
        evidence = str(item.get("evidence") or "").strip()[:500]
        fp = fingerprint(gid, title, owner)
        row = Todo.objects.filter(fp=fp).first()
        if row is not None:
            if row.status in ("done", "dropped"):
                continue
            row.due = due or row.due
            row.priority = priority
            row.evidence = evidence or row.evidence
            row.run_id = int(run_id)
            row.save()
            continue
        Todo.objects.create(
            group_id=gid, fp=fp, title=title[:200], owner=owner, due=due,
            priority=priority, status="open", evidence=evidence,
            run_id=int(run_id), created_at=stamp,
        )
        created += 1
    total = Todo.objects.filter(group_id=gid, status="open").count()
    return created, total


def close_todo(todo_id: int, *, user_id: int = 0, status: str = "done",
               now: Optional[int] = None) -> Optional[Todo]:
    """把一条待办标记为完成 / 忽略。返回改后的行；不存在返回 None。"""
    row = get_todo(todo_id)
    if row is None:
        return None
    row.status = status
    row.done_at = int(now if now is not None else time.time())
    row.done_by = int(user_id or 0)
    row.save()
    return row


# ------------------------------------------------------------------ 运行记录

def record_run(group_id, *, start_ts: int, end_ts: int, msg_count: int,
               todo_new: int, todo_open: int, model: str, cost_ms: int,
               now: Optional[int] = None) -> Run:
    return Run.objects.create(
        group_id=int(group_id), start_ts=int(start_ts), end_ts=int(end_ts),
        msg_count=int(msg_count), todo_new=int(todo_new), todo_open=int(todo_open),
        model=str(model or "")[:64], cost_ms=int(cost_ms),
        created_at=int(now if now is not None else time.time()),
    )


def last_run(group_id) -> Optional[Run]:
    return Run.objects.filter(group_id=int(group_id)).order_by("-id").first()


__all__ = [
    "Msg", "Todo", "Run", "SyncState",
    "normalize_title", "fingerprint",
    "existing_message_ids", "save_messages", "messages_between", "message_count",
    "latest_message_ts", "purge_messages",
    "sync_state", "touch_sync",
    "open_todos", "get_todo", "upsert_todos", "close_todo",
    "record_run", "last_run",
]
