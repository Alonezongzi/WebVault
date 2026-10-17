"""通用工具函数。

保持纯标准库。这里的每个函数都要求对脏数据容错 —— 网页是最脏的数据。
"""

from __future__ import annotations

import base64
import hashlib
import html as _html
import os
import re
import unicodedata
from typing import Iterable, Optional
from urllib.parse import urljoin, urlsplit, urlunsplit

# --------------------------------------------------------------------------
# 文本处理
# --------------------------------------------------------------------------

_WS_RUN = re.compile(r"[ \t\r\f\v]+")
_BLANK_RUN = re.compile(r"\n{3,}")
_TAG_LIKE = re.compile(r"<[^>]{0,4000}>")


def collapse_ws(text: str) -> str:
    """把连续空白压成单个空格（不动换行）。"""
    if not text:
        return ""
    return _WS_RUN.sub(" ", text).strip()


def normalize_text(text: str) -> str:
    """正文用归一化：Unicode 规范化、去零宽字符、压空白、压空行。"""
    if not text:
        return ""
    # 去掉零宽与 BOM 类字符，它们会让内容里的判断莫名失败
    text = text.replace("\u200b", "").replace("\ufeff", "").replace("\u00ad", "")
    text = text.replace("\u200e", "").replace("\u200f", "")
    try:
        text = unicodedata.normalize("NFKC", text)
    except Exception:
        pass
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _WS_RUN.sub(" ", text)
    text = _BLANK_RUN.sub("\n\n", text)
    return text.strip()


def strip_tags(text: str) -> str:
    """粗暴去标签，仅用于不信任输入的最后兜底。"""
    return _TAG_LIKE.sub("", text or "")


def unescape(text: str) -> str:
    """HTML 实体解码，容错。"""
    if not text:
        return ""
    try:
        return _html.unescape(text)
    except Exception:
        return text


def escape(text: str) -> str:
    """HTML 转义（用于输出到 HTML 的文本节点）。"""
    return _html.escape(text or "", quote=True)


def truncate(text: str, limit: int, suffix: str = "…") -> str:
    if not text or len(text) <= limit:
        return text or ""
    return text[: max(0, limit - len(suffix))].rstrip() + suffix


def slugify(text: str, max_len: int = 60) -> str:
    """把标题变成安全的文件名片段。保留中文。"""
    text = normalize_text(text or "")
    # 去掉文件系统不友好的字符
    bad = '<>:"/\\|?*'
    out = []
    for ch in text:
        if ch in bad or ord(ch) < 32:
            out.append("_")
        elif ch.isspace():
            out.append("-")
        else:
            out.append(ch)
    s = "".join(out)
    s = re.sub(r"[-_]{2,}", "-", s).strip("-_. ")
    if not s:
        s = "untitled"
    if len(s) > max_len:
        s = s[:max_len].rstrip("-_. ")
    return s or "untitled"


# --------------------------------------------------------------------------
# URL 处理
# --------------------------------------------------------------------------

def safe_urljoin(base: str, url: str) -> str:
    """urljoin 的容错版本：任何异常都退回原 url。"""
    if not url:
        return ""
    if not base:
        return url
    try:
        return urljoin(base, url.strip())
    except Exception:
        return url


def url_origin(url: str) -> str:
    try:
        p = urlsplit(url)
        return urlunsplit((p.scheme, p.netloc, "", "", ""))
    except Exception:
        return ""


def url_host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except Exception:
        return ""


def url_scheme(url: str) -> str:
    try:
        return (urlsplit(url).scheme or "").lower()
    except Exception:
        return ""


def is_http(url: str) -> bool:
    return url_scheme(url) in ("http", "https")


def is_fetchable(url: str) -> bool:
    """判断一个 URL 是否值得去抓。"""
    if not url:
        return False
    s = url_scheme(url)
    if s in ("data", "blob", "javascript", "mailto", "tel", "about"):
        return False
    return s in ("http", "https") or url.startswith("//")


def normalize_url_for_dedup(url: str) -> str:
    """去掉 fragment 和常见追踪参数，用于去重。"""
    if not url:
        return ""
    try:
        p = urlsplit(url)
    except Exception:
        return url
    drop_prefixes = ("utm_", "fbclid", "gclid", "spm", "from", "share_")
    kept = []
    for pair in (p.query or "").split("&"):
        if not pair:
            continue
        key = pair.split("=", 1)[0].lower()
        if any(key.startswith(x) for x in drop_prefixes):
            continue
        kept.append(pair)
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path, "&".join(kept), ""))


# --------------------------------------------------------------------------
# 编码 / 二进制
# --------------------------------------------------------------------------

def b64encode(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def data_uri(data: bytes, mime: str) -> str:
    """构造 data URI。文本类做 URL-safe base64，二进制直接 base64。"""
    mime = mime or "application/octet-stream"
    if not data:
        return ""
    # SVG 用 URL 编码更省体积，但 base64 更稳；统一走 base64
    return "data:%s;base64,%s" % (mime, b64encode(data))


def sha1_short(data, length: int = 12) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8", "replace")
    return hashlib.sha1(data).hexdigest()[:length]


def human_size(n: int) -> str:
    """1536 -> '1.5 KB'"""
    try:
        n = float(n)
    except Exception:
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            if unit == "B":
                return "%d B" % int(n)
            return "%.1f %s" % (n, unit)
        n /= 1024.0
    return "%.1f GB" % n


# --------------------------------------------------------------------------
# 其他
# --------------------------------------------------------------------------

def first_of(seq: Iterable, default=None):
    for item in seq:
        return item
    return default


def coerce_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def coerce_bool(value, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on", "y", "t")


def ensure_dir(path: str) -> str:
    if path and not os.path.isdir(path):
        os.makedirs(path, exist_ok=True)
    return path
