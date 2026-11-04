# -*- coding: utf-8 -*-
"""图片与附件的取用：下载、限流、解析。

三件事：

1. **标注**（`tag_rows`）—— 把消息文本里的 `[图片]` / `[文件:xxx]` 换成
   `[图1]` / `[文件1:xxx]`，这样模型能把附件和「谁在什么时候发的」对起来；
2. **下载**（`_read_media`）—— **先走框架的流式下载**（`download_file_image_stream`
   / `download_file_stream`，让 NapCat 那边去取，它就在 QQ 环境里），
   拿不到才自己直连兜底。边下边计数，超限立刻掐断；
3. **解析**（`extract_text`）—— 按扩展名分流；PDF/Word/Excel 用可选依赖，
   没装就如实说明「解析不了」，不假装成功。

为什么不能自己拉 URL：QQ 的图片链带防盗链（403）、群文件链要 cookie（401），
消息段里的 `file` 又常是 `file://` 本地路径 —— aiohttp 一个都搞不定。
这是最初版本图片/文件全挂的根因。

尺寸上限、单次数量上限、缓存时长都在 `config.json → plugins.qqnotice_sum.media`。
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import re
import time
from pathlib import Path
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


async def _fetch(url: str, *, max_bytes: int, timeout: float) -> tuple[Optional[bytes], str]:
    """直连下载，超过 `max_bytes` 立即放弃。

    **只当兜底用**：QQ 的图片链带防盗链、群文件链要 cookie，直连经常 401/403；
    `file://` 路径更是拉不到。真正干活的是 `_read_via_stream`。
    """
    if not url:
        return None, "没有可用的下载地址"
    client_timeout = aiohttp.ClientTimeout(total=float(timeout))
    try:
        async with aiohttp.ClientSession(timeout=client_timeout) as session:
            async with session.get(url) as resp:
                if resp.status == 403:
                    return None, "直连被拒（HTTP 403，多半是防盗链）"
                if resp.status >= 400:
                    return None, f"直连失败（HTTP {resp.status}）"
                length = resp.headers.get("Content-Length")
                if length and length.isdigit() and int(length) > max_bytes:
                    return None, "超过大小上限"
                buf = bytearray()
                async for chunk in resp.content.iter_chunked(65536):
                    buf.extend(chunk)
                    if len(buf) > max_bytes:
                        return None, "超过大小上限"   # 超限：直接掐断
                return bytes(buf), "ok"
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
        return None, f"直连出错（{type(e).__name__}）"


async def _drain(stream, max_bytes: int) -> Optional[bytes]:
    """把一个异步字节流读完，超限立刻停并关闭它。"""
    buf = bytearray()
    try:
        async for chunk in stream:
            if not chunk:
                continue
            buf.extend(chunk)
            if max_bytes and len(buf) > max_bytes:
                return None
    finally:
        close = getattr(stream, "aclose", None)
        if close is not None:
            try:
                await close()
            except Exception:               # noqa: BLE001 - 关流失败不该影响主流程
                pass
    return bytes(buf)


def _stream_candidates(plugin, descriptor: dict, *, kind: str):
    """列出「流式读」的候选调用，按可靠性排序。

    文档 §11.1 的标准入口是 `self.files`（FileManager），§11.3 也说低层的
    `self.api.download_file_*` 可以直接用。但**不是每个部署版本两个都在** ——
    所以都探测一遍，谁在使用谁；一个都没有就返回空，上层降级到 `download_file`。
    """
    api = getattr(plugin, "api", None)
    files = getattr(plugin, "files", None) or getattr(plugin, "file_manager", None)

    file_id = str(descriptor.get("file_id") or "").strip() or None
    file_ref = str(descriptor.get("file") or "").strip() or None
    url = str(descriptor.get("url") or "").strip() or None

    # 按「谁最可能认这个标识」排：图片认 file_id/file，文件认直链
    raw = ([{"file_id": file_id}, {"file": file_ref}, {"file": url}]
           if kind == "image" else
           [{"file": url}, {"file_id": file_id}, {"file": file_ref}])
    targets = [item for item in raw if any(item.values())]

    out: list[tuple] = []
    for kwargs in targets:
        if api is not None:
            name = ("download_file_image_stream" if kind == "image"
                    else "download_file_stream")
            fn = getattr(api, name, None)
            if fn is not None:
                out.append(("api", fn, kwargs))
        if files is not None:
            if kind == "image":
                fn = getattr(files, "download_image_stream", None)
                if fn is not None:
                    out.append(("kw", fn, kwargs))
            else:
                # `FileManager.download_stream(url, *, file_id=...)` 第一个是位置参数
                fn = getattr(files, "download_stream", None)
                if fn is not None:
                    out.append(("url", fn, kwargs))
    return out


def _call_stream(mode: str, fn, kwargs: dict, timeout: float):
    """三种入口的签名不一样，这里抹平。返回异步字节迭代器。"""
    if mode == "api":
        return fn(timeout=float(timeout), **kwargs)
    if mode == "url":                      # FileManager.download_stream(url, *, file_id)
        target = kwargs.get("file")
        rest = {k: v for k, v in kwargs.items() if k != "file"}
        return fn(target, timeout=float(timeout), **rest)
    return fn(**kwargs)                    # FileManager.download_image_stream(*, file, file_id)


async def _read_via_stream(plugin, descriptor: dict, *, kind: str,
                           max_bytes: int, timeout: float) -> tuple[Optional[bytes], str]:
    """流式读：让 NapCat 那边边下边回传，我们这头边收边计数。

    这是**首选**：不落盘、超限能立刻掐断，而且能处理 `file://` 路径和
    带防盗链的图片链（我们直连拿不到）。
    """
    entries = _stream_candidates(plugin, descriptor, kind=kind)
    if not entries:
        return None, "当前框架版本没有流式下载接口"

    last = "没有可用的文件标识"
    for mode, fn, kwargs in entries:
        try:
            blob = await _drain(_call_stream(mode, fn, kwargs, timeout), max_bytes)
        except Exception as e:              # noqa: BLE001 - 换下一个入口/标识再试
            last = f"{type(e).__name__}: {str(e)[:80]}"
            continue
        if blob is None:
            last = "超过大小上限"
            continue
        if blob:
            return blob, "ok"
        last = "取回来是空的"
    return None, last


async def _read_via_download_file(api, url: str, *, max_bytes: int,
                                  name: str = "") -> tuple[Optional[bytes], str]:
    """老框架的兜底：`download_file`。

    这是 NapCat 很早就有的接口，比流式下载普及得多。它把文件下到 **NapCat
    那台机器**上，返回 `file` 路径（同机部署时能直接读）。没有流式接口的部署
    靠这条活着。
    """
    if not url:
        return None, "没有可用的下载地址"
    download = getattr(api, "download_file", None)
    if download is None:
        return None, "框架没有 download_file"

    try:
        info = await download(url=url, name=name or "file")
    except Exception as e:                 # noqa: BLE001
        return None, f"download_file 失败（{type(e).__name__}: {str(e)[:60]}）"

    def _get(key):
        if isinstance(info, dict):
            return info.get(key)
        return getattr(info, key, None)

    encoded = _get("base64")
    if encoded:
        try:
            blob = base64.b64decode(encoded)
        except Exception:                  # noqa: BLE001
            blob = b""
        if blob:
            if max_bytes and len(blob) > max_bytes:
                return None, "超过大小上限"
            return blob, "ok"

    path = _get("file")
    if path:
        try:
            target = Path(str(path))
            if target.is_file():
                if max_bytes and target.stat().st_size > max_bytes:
                    return None, "超过大小上限"
                return target.read_bytes(), "ok"
        except OSError:
            pass
    return None, "download_file 没给回内容（可能框架与 NapCat 不在同一台机器）"


async def _read_media(plugin, group_id, descriptor: dict, *, kind: str,
                      max_bytes: int, timeout: float) -> tuple[Optional[bytes], str]:
    """取附件字节，三级降级。

    1. **流式**（`self.files` / `self.api.download_file_*`）—— 首选；
    2. **`download_file`** —— 老框架也有的接口，靠它兜住没流式的部署；
    3. **直连 URL** —— 最后的兜底，公网 CDN 图片偶尔能成。

    为什么不自己 HTTP 拉：QQ 的图片链带防盗链（403）、群文件链要 cookie（401）、
    消息段里的 `file` 常常是 `file://` 本地路径。这三样 aiohttp 一个都搞不定。
    """
    target = dict(descriptor)
    if not str(target.get("url") or "").strip():
        # 先把 url 换出来：文件走 get_group_file_url（带鉴权参数），
        # 图片走 get_image。第 2、3 级都靠它。
        got = (await _image_url(plugin, descriptor) if kind == "image"
               else await _file_url(plugin, group_id, descriptor))
        if got:
            target["url"] = got

    reasons: list[str] = []

    blob, reason = await _read_via_stream(plugin, target, kind=kind,
                                          max_bytes=max_bytes, timeout=timeout)
    if blob:
        return blob, "ok"
    reasons.append(f"流式：{reason}")

    api = getattr(plugin, "api", None)
    url = str(target.get("url") or "").strip()
    if api is not None and url:
        name = str(descriptor.get("name") or "").strip()
        blob, reason = await _read_via_download_file(api, url,
                                                    max_bytes=max_bytes, name=name)
        if blob:
            return blob, "ok"
        reasons.append(f"download_file：{reason}")

    if url:
        blob, reason = await _fetch(url, max_bytes=max_bytes, timeout=timeout)
        if blob:
            return blob, "ok"
        reasons.append(f"直连：{reason}")
    else:
        reasons.append("没有可用的下载地址")

    return None, "；".join(reasons)


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
        return _url_of(info)
    except Exception:                       # noqa: BLE001 - 拿不到就跳过这张图
        return ""


def _url_of(info) -> str:
    """`FileInfo`（pydantic）或 dict，取 url。"""
    if info is None:
        return ""
    if isinstance(info, dict):
        return str(info.get("url") or "")
    return str(getattr(info, "url", "") or "")


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
            got = _url_of(data)
            if got:
                return got
        info = await api.get_file(file=descriptor.get("file") or None,
                                  file_id=file_id or None)
        return _url_of(info)
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
            blob, reason = await _read_media(
                plugin, group_id, descriptor, kind="image",
                max_bytes=image_bytes or 5 * 1024 * 1024, timeout=timeout)
            if not blob:
                result["notes"].append(f"{label} 没取到：{reason}")
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
        blob, reason = await _read_media(
            plugin, group_id, descriptor, kind="file",
            max_bytes=file_bytes or 5 * 1024 * 1024, timeout=timeout)
        if not blob:
            result["skipped"].append(f"{label}（{reason}）")
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
