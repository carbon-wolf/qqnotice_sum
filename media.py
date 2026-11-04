# -*- coding: utf-8 -*-
"""图片与附件的取用：下载、限流、解析。

三件事：

1. **标注**（`tag_rows`）—— 把消息文本里的 `[图片]` / `[文件:xxx]` 换成
   `[图1]` / `[文件1:xxx]`，这样模型能把附件和「谁在什么时候发的」对起来；
2. **下载**（`_fetch`）—— 流式读、超限立刻中止，绝不让一个 200MB 的压缩包
   把内存和额度一起吃掉；
3. **解析**（`extract_text`）—— 按扩展名分流；PDF/Word/Excel 用可选依赖，
   没装就如实说明「解析不了」，不假装成功。

尺寸上限、单次数量上限、缓存时长都在 `config.json → plugins.qqnotice_sum.media`。
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import re
import time
from typing import Any, Optional

import aiohttp

from . import store

#: 纯文本类扩展名：直接解码
TEXT_EXT = {
    ".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".log", ".ini", ".cfg",
    ".yaml", ".yml", ".xml", ".html", ".htm", ".py", ".js", ".ts", ".java",
    ".c", ".cpp", ".h", ".sql", ".sh", ".bat", ".tex",
}

_IMAGE_MAGIC = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"RIFF", "image/webp"),          # RIFF....WEBP
)


def human_size(size: int) -> str:
    try:
        num = float(size or 0)
    except (TypeError, ValueError):
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if num < 1024 or unit == "GB":
            return f"{num:.0f}{unit}" if unit == "B" else f"{num:.1f}{unit}"
        num /= 1024
    return f"{num:.1f}GB"


# ------------------------------------------------------------------ 标注

#: 消息文本里的占位符（与 collect 保持一致）
_IMAGE_TOKEN = "[图片]"
_FILE_TOKEN_RE = re.compile(r"\[文件(?::([^\]]*))?\]")


def tag_rows(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """把附件占位符换成带编号的形式，并按编号顺序返回附件清单。

    Returns:
        `(tagged_rows, items)`，`items` 每项：`{kind, label, index, descriptor, row}`
        —— `index` 是这条消息里第几个附件（缓存键要用）。
    """
    tagged: list[dict] = []
    items: list[dict] = []
    image_no = 0
    file_no = 0
    for row in rows:
        media = row.get("media") or []
        text = str(row.get("text") or "")
        if not media:
            tagged.append(row)
            continue
        new_row = dict(row)
        for index, item in enumerate(media):
            if not isinstance(item, dict):
                continue
            if item.get("kind") == "image":
                image_no += 1
                label = f"图{image_no}"
                text = text.replace(_IMAGE_TOKEN, f"[{label}]", 1)
                items.append({"kind": "image", "label": label, "index": index,
                              "descriptor": item, "row": row})
            else:
                file_no += 1
                name = str(item.get("name") or "附件").strip()
                label = f"文件{file_no}"
                text = _FILE_TOKEN_RE.sub(f"[{label}:{name}]", text, count=1)
                items.append({"kind": "file", "label": label, "index": index,
                              "descriptor": item, "row": row})
        new_row["text"] = text
        tagged.append(new_row)
    return tagged, items


# ------------------------------------------------------------------ 下载

def _mime_of(blob: bytes) -> str:
    for magic, mime in _IMAGE_MAGIC:
        if blob.startswith(magic):
            if mime == "image/webp" and blob[8:12] != b"WEBP":
                continue
            return mime
    return "image/jpeg"


async def _fetch(url: str, *, max_bytes: int, timeout: float) -> Optional[bytes]:
    """流式下载，超过 `max_bytes` 立即放弃（返回 None）。"""
    if not url:
        return None
    client_timeout = aiohttp.ClientTimeout(total=float(timeout))
    try:
        async with aiohttp.ClientSession(timeout=client_timeout) as session:
            async with session.get(url) as resp:
                if resp.status >= 400:
                    return None
                length = resp.headers.get("Content-Length")
                if length and length.isdigit() and int(length) > max_bytes:
                    return None
                buf = bytearray()
                async for chunk in resp.content.iter_chunked(65536):
                    buf.extend(chunk)
                    if len(buf) > max_bytes:
                        return None            # 超限：直接掐断，别继续下
                return bytes(buf)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        return None


async def _image_url(plugin, descriptor: dict) -> str:
    """图片直链：段里带了就用，没带再问一次接口。"""
    url = str(descriptor.get("url") or "").strip()
    if url:
        return url
    api = getattr(plugin, "api", None)
    if api is None:
        return ""
    try:
        info = await api.get_image(file=descriptor.get("file") or None,
                                   file_id=descriptor.get("file_id") or None)
        return str(getattr(info, "url", "") or (info.get("url") if isinstance(info, dict) else "") or "")
    except Exception:                       # noqa: BLE001 - 拿不到就跳过这张图
        return ""


async def _file_url(plugin, group_id, descriptor: dict) -> str:
    url = str(descriptor.get("url") or "").strip()
    if url:
        return url
    api = getattr(plugin, "api", None)
    if api is None:
        return ""
    file_id = str(descriptor.get("file_id") or "")
    try:
        if file_id:
            data = await api.get_group_file_url(str(group_id), file_id)
            if isinstance(data, dict):
                return str(data.get("url") or "")
        info = await api.get_file(file=descriptor.get("file") or None, file_id=file_id or None)
        if isinstance(info, dict):
            return str(info.get("url") or "")
        return str(getattr(info, "url", "") or "")
    except Exception:                       # noqa: BLE001
        return ""


# ------------------------------------------------------------------ 解析

def extract_text(name: str, blob: bytes, *, max_chars: int) -> tuple[str, str]:
    """`(提取到的文本, 状态)`。状态：ok / empty / unsupported / need_dep。"""
    ext = ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else ""
    if ext in TEXT_EXT:
        text = _decode(blob)
        return (text[:max_chars], "ok" if text.strip() else "empty")
    if ext == ".pdf":
        return _extract_pdf(blob, max_chars)
    if ext == ".docx":
        return _extract_docx(blob, max_chars)
    if ext == ".xlsx":
        return _extract_xlsx(blob, max_chars)
    return "", "unsupported"


def _decode(blob: bytes) -> str:
    for encoding in ("utf-8", "utf-8-sig", "gb18030"):
        try:
            return blob.decode(encoding)
        except UnicodeDecodeError:
            continue
    return blob.decode("utf-8", errors="ignore")


def _extract_pdf(blob: bytes, max_chars: int) -> tuple[str, str]:
    try:
        from pypdf import PdfReader            # type: ignore
    except ImportError:
        try:
            from PyPDF2 import PdfReader       # type: ignore
        except ImportError:
            return "", "need_dep"
    try:
        reader = PdfReader(io.BytesIO(blob))
        chunks = []
        for page in reader.pages[:20]:         # 只读前 20 页，够判断通知内容
            chunks.append(page.extract_text() or "")
            if sum(len(c) for c in chunks) > max_chars:
                break
        text = "\n".join(chunks)
        return text[:max_chars], ("ok" if text.strip() else "empty")
    except Exception:                          # noqa: BLE001 - 坏 PDF 不该炸整个总结
        return "", "error"


def _extract_docx(blob: bytes, max_chars: int) -> tuple[str, str]:
    try:
        import docx                            # type: ignore
    except ImportError:
        return "", "need_dep"
    try:
        document = docx.Document(io.BytesIO(blob))
        parts = [p.text for p in document.paragraphs if p.text.strip()]
        for table in document.tables:
            for row in table.rows:
                cells = [c.text.strip() for c in row.cells if c.text.strip()]
                if cells:
                    parts.append(" | ".join(cells))
        text = "\n".join(parts)
        return text[:max_chars], ("ok" if text.strip() else "empty")
    except Exception:                          # noqa: BLE001
        return "", "error"


def _extract_xlsx(blob: bytes, max_chars: int) -> tuple[str, str]:
    try:
        import openpyxl                        # type: ignore
    except ImportError:
        return "", "need_dep"
    try:
        book = openpyxl.load_workbook(io.BytesIO(blob), read_only=True, data_only=True)
        parts = []
        for sheet in book.worksheets[:3]:
            parts.append(f"# {sheet.title}")
            for i, row in enumerate(sheet.iter_rows(values_only=True)):
                if i > 50:
                    break
                cells = [str(c) for c in row if c is not None]
                if cells:
                    parts.append(" | ".join(cells))
                if sum(len(p) for p in parts) > max_chars:
                    break
        text = "\n".join(parts)
        return text[:max_chars], ("ok" if text.strip() else "empty")
    except Exception:                          # noqa: BLE001
        return "", "error"


# ------------------------------------------------------------------ 汇总

async def gather(plugin, group_id: int, items: list[dict], *, cfg: dict) -> dict:
    """把附件清单变成「给模型看的东西」。

    Returns:
        `{"images": [{"label","data_uri"}], "files": [{"label","name","text","status"}],
          "skipped": [...], "notes": [...]}`
    """
    result = {"images": [], "files": [], "skipped": [], "notes": []}
    if not items:
        return result
    image_cfg = cfg.get("image") or {}
    file_cfg = cfg.get("file") or {}
    allow_ext = {str(e).lower() for e in (file_cfg.get("allow_ext") or [])}

    image_budget = int(image_cfg.get("max_per_run") or 0)
    file_budget = int(file_cfg.get("max_per_run") or 0)
    image_bytes = int(image_cfg.get("max_bytes") or 0)
    file_bytes = int(file_cfg.get("max_bytes") or 0)
    file_chars = int(file_cfg.get("max_chars") or 4000)
    timeout = float(cfg.get("timeout") or 45)

    for item in items:
        descriptor = item.get("descriptor") or {}
        label = item.get("label") or "附件"
        size = int(descriptor.get("size") or 0)
        kind = item.get("kind")

        if kind == "image":
            if not image_cfg.get("enabled", True):
                continue
            if image_budget <= 0:
                result["skipped"].append(f"{label}（本次图片数已达上限）")
                continue
            if size and image_bytes and size > image_bytes:
                result["skipped"].append(f"{label}（{human_size(size)}，超过单张上限）")
                continue
            url = await _image_url(plugin, descriptor)
            blob = await _fetch(url, max_bytes=image_bytes or 5 * 1024 * 1024,
                                timeout=timeout)
            if not blob:
                result["notes"].append(f"{label} 取不到（可能是外链失效）")
                continue
            image_budget -= 1
            result["images"].append({
                "label": label,
                "data_uri": f"data:{_mime_of(blob)};base64,"
                            + base64.b64encode(blob).decode("ascii"),
            })
            continue

        # ---- 文件
        if not file_cfg.get("enabled", True):
            continue
        name = str(descriptor.get("name") or "附件").strip()
        ext = ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else ""
        if allow_ext and ext not in allow_ext:
            result["skipped"].append(f"{label}（{ext or '无扩展名'} 不在允许列表）")
            continue
        if size and file_bytes and size > file_bytes:
            result["skipped"].append(f"{label}（{human_size(size)}，超过文件上限）")
            continue

        cached = _cached(group_id, item, cfg)
        if cached is not None:
            result["files"].append(cached)
            file_budget = max(0, file_budget - 1)
            continue

        if file_budget <= 0:
            result["skipped"].append(f"{label}（本次文件数已达上限）")
            continue
        url = await _file_url(plugin, group_id, descriptor)
        blob = await _fetch(url, max_bytes=file_bytes or 5 * 1024 * 1024, timeout=timeout)
        if not blob:
            result["skipped"].append(f"{label}（下载失败或超过上限）")
            _cache(group_id, item, "", "error", size)
            continue
        file_budget -= 1
        text, status = extract_text(name, blob, max_chars=file_chars)
        _cache(group_id, item, text, status, len(blob))
        result["files"].append({"label": label, "name": name, "text": text,
                                "status": status, "size": len(blob)})

    return result


def cache_key(group_id: int, descriptor: dict) -> str:
    """缓存键按**文件本身**算，不按消息位置。

    同一份文件被转发/被合并到不同消息里，键都一样，只下载一次；
    也不会因为合并边界变了就失效。
    """
    raw = "|".join(str(descriptor.get(k) or "") for k in ("file_id", "file", "url", "name"))
    return hashlib.md5(f"{int(group_id)}|{raw}".encode("utf-8")).hexdigest()


def _cached(group_id: int, item: dict, cfg: dict) -> Optional[dict]:
    """命中的缓存直接返回；过期或状态为 error 的重试。"""
    try:
        row = store.media_cache_get(cache_key(group_id, item.get("descriptor") or {}))
    except Exception:                        # noqa: BLE001 - 缓存坏了不该挡住总结
        return None
    if row is None:
        return None
    ttl_hours = int(cfg.get("cache_hours") or 72)
    if ttl_hours > 0 and row.fetched_at and (int(time.time()) - int(row.fetched_at)) > ttl_hours * 3600:
        return None
    if row.status == "error":
        return None
    name = str(row.name or "附件")
    return {"label": item.get("label") or "文件", "name": name,
            "text": row.text or "", "status": row.status or "empty",
            "size": int(row.size or 0)}


def _cache(group_id: int, item: dict, text: str, status: str, size: int) -> None:
    row = item.get("row") or {}
    descriptor = item.get("descriptor") or {}
    try:
        store.media_cache_put(
            key=cache_key(group_id, descriptor),
            message_id=int(row.get("message_id") or 0),
            group_id=int(group_id),
            kind=str(descriptor.get("kind") or "file"),
            name=str(descriptor.get("name") or ""),
            size=int(size or 0),
            text=text, status=status)
    except Exception:                        # noqa: BLE001
        pass


def build_file_section(files: list[dict]) -> str:
    """把提取到的文件文本拼成提示词里的一段。"""
    if not files:
        return ""
    blocks = ["", "【聊天里提到的附件内容】"]
    for item in files:
        label = item.get("label")
        name = item.get("name")
        status = item.get("status")
        if status == "ok":
            blocks.append(f"--- {label} {name} ---")
            blocks.append(str(item.get("text") or "").strip())
        elif status == "empty":
            blocks.append(f"--- {label} {name} ---（解析出来是空的，可能是扫描件）")
        elif status == "need_dep":
            blocks.append(f"--- {label} {name} ---（格式需要额外依赖才能解析，先忽略）")
        elif status == "unsupported":
            blocks.append(f"--- {label} {name} ---（不支持解析这类文件，只知道文件名）")
        else:
            blocks.append(f"--- {label} {name} ---（读取失败）")
    return "\n".join(blocks)


def image_note(images: list[dict]) -> str:
    if not images:
        return ""
    labels = "、".join(item["label"] for item in images)
    return (f"\n\n另外附上聊天里出现的图片（{labels}），编号与聊天记录里的 [图N] 一一对应，"
            "请把它们当作通知的一部分一起理解。")


__all__ = [
    "TEXT_EXT", "human_size", "tag_rows", "extract_text", "gather", "cache_key",
    "build_file_section", "image_note",
]
