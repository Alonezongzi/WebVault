"""变更追踪 —— 同一篇文章两次抓取之间，到底改了什么。

场景：你关心的页面（公告、价格、文档）想知道它什么时候悄悄改了。
单纯"存下来"回答不了这个问题，你需要的是"上次是什么样，这次哪里不一样"。

做法：
  每次保存时，把提取出来的**纯文本正文**按 URL 记一份到
  `<保存目录>/.webvault/` 下（一个 json 索引 + 每个版本一个 txt）。
  下次抓同一个 URL 时，把新正文和上次的比一比，给出行级差异。

用 difflib（标准库）做差异，所以仍然是零依赖的。

设计取舍：
  - 只存纯文本，不存 HTML。HTML 里的时间戳、广告位、随机 id 会让每次
    抓取都"看起来变了"，纯正文比对才有意义。
  - 按 URL 归一化后的键存储，`?utm_source=` 这类跟踪参数不会产生新版本。
  - 历史放在隐藏目录里，不污染保存目录；书库扫描会跳过它。
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import time
from typing import Dict, List, Optional, Tuple

from .util import normalize_url_for_dedup, truncate

HISTORY_DIR = ".webvault"
TEXT_SUBDIR = "text"
MAX_VERSIONS = 20          # 每个 URL 最多留多少个历史版本
MAX_TEXT_BYTES = 2 * 1024 * 1024   # 单个版本正文上限


# --------------------------------------------------------------------------
# 路径
# --------------------------------------------------------------------------

def history_dir(out_dir: str) -> str:
    return os.path.join(out_dir, HISTORY_DIR)


def history_file(out_dir: str) -> str:
    return os.path.join(history_dir(out_dir), "history.json")


def text_dir(out_dir: str) -> str:
    return os.path.join(history_dir(out_dir), TEXT_SUBDIR)


def key_for(url: str) -> str:
    """URL → 历史键。去掉查询串里的跟踪参数，避免每次都是"新页面"。"""
    norm = normalize_url_for_dedup(url) or url
    return norm.strip()


# --------------------------------------------------------------------------
# 读写
# --------------------------------------------------------------------------

def load_history(out_dir: str) -> Dict:
    path = history_file(out_dir)
    if not os.path.isfile(path):
        return {"version": 1, "pages": {}}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or "pages" not in data:
            return {"version": 1, "pages": {}}
        return data
    except Exception:
        # 索引坏了不应该让整个流程失败，重来一份就好
        return {"version": 1, "pages": {}}


def save_history(out_dir: str, hist: Dict) -> None:
    d = history_dir(out_dir)
    os.makedirs(d, exist_ok=True)
    tmp = history_file(out_dir) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(hist, f, ensure_ascii=False, indent=1)
    os.replace(tmp, history_file(out_dir))


def _text_path(out_dir: str, digest: str) -> str:
    return os.path.join(text_dir(out_dir), digest[:2], digest + ".txt")


def read_version_text(out_dir: str, digest: str) -> str:
    p = _text_path(out_dir, digest)
    if not os.path.isfile(p):
        return ""
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            return f.read(MAX_TEXT_BYTES)
    except Exception:
        return ""


def _write_version_text(out_dir: str, digest: str, text: str) -> None:
    p = _text_path(out_dir, digest)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    if not os.path.isfile(p):
        with open(p, "w", encoding="utf-8") as f:
            f.write(text[:MAX_TEXT_BYTES])


def digest_of(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()


# --------------------------------------------------------------------------
# 记录
# --------------------------------------------------------------------------

def record(out_dir: str, url: str, title: str, text: str,
           file_path: str = "") -> Optional[Dict]:
    """记录一次抓取。返回**上一次**的版本信息（没有则 None）。

    这是"变更检测"的核心：调用方拿到上一版之后，可以自己去比对。
    """
    if not out_dir or not url or not text:
        return None
    key = key_for(url)
    hist = load_history(out_dir)
    pages = hist.setdefault("pages", {})
    prev = pages.get(key)

    digest = digest_of(text)
    _write_version_text(out_dir, digest, text)

    versions = list((prev or {}).get("versions") or [])
    if not versions or versions[-1].get("hash") != digest:
        versions.append({
            "hash": digest,
            "captured": time.strftime("%Y-%m-%d %H:%M:%S"),
            "bytes": len(text.encode("utf-8", "replace")),
            "file": os.path.basename(file_path) if file_path else "",
        })
    versions = versions[-MAX_VERSIONS:]

    pages[key] = {
        "title": title or (prev or {}).get("title", ""),
        "last_seen": time.strftime("%Y-%m-%d %H:%M:%S"),
        "file": file_path or (prev or {}).get("file", ""),
        "versions": versions,
    }
    save_history(out_dir, hist)
    return prev


def last_text(out_dir: str, url: str, *, before_hash: str = "") -> str:
    """取这个 URL 最近一版正文。before_hash 用来跳过刚写入的当前版本。"""
    hist = load_history(out_dir)
    entry = (hist.get("pages") or {}).get(key_for(url)) or {}
    versions = entry.get("versions") or []
    for v in reversed(versions):
        h = v.get("hash") or ""
        if before_hash and h == before_hash:
            continue
        t = read_version_text(out_dir, h)
        if t:
            return t
    return ""


def history_for(out_dir: str, url: str) -> Dict:
    hist = load_history(out_dir)
    return (hist.get("pages") or {}).get(key_for(url)) or {}


def known_pages(out_dir: str) -> List[Dict]:
    hist = load_history(out_dir)
    out = []
    for url, entry in (hist.get("pages") or {}).items():
        vs = entry.get("versions") or []
        out.append({
            "url": url,
            "title": entry.get("title", ""),
            "last_seen": entry.get("last_seen", ""),
            "versions": len(vs),
            "file": entry.get("file", ""),
        })
    out.sort(key=lambda x: x.get("last_seen") or "", reverse=True)
    return out


# --------------------------------------------------------------------------
# 差异
# --------------------------------------------------------------------------

def normalize_for_diff(text: str) -> List[str]:
    """把正文切成用于比较的行。

    按句子断行而不是按原始换行 —— 网页正文经常整段没有换行符，
    按原文比较的话，改一个字就会让"整段"变成一行差异，看不出改了哪里。
    """
    if not text:
        return []
    t = text.replace("\r\n", "\n").replace("\r", "\n")
    lines: List[str] = []
    for para in re.split(r"\n+", t):
        para = para.strip()
        if not para:
            continue
        # 中文按句号类标点断句；英文按句末标点
        parts = re.split(r"(?<=[。！？!?；;])", para)
        buf = ""
        for p in parts:
            if not p:
                continue
            if len(buf) + len(p) <= 60:
                buf += p
            else:
                if buf:
                    lines.append(buf.strip())
                buf = p
        if buf.strip():
            lines.append(buf.strip())
    return [l for l in lines if l.strip()]


def diff_texts(old: str, new: str, *, context: int = 2,
               max_lines: int = 400) -> Tuple[List[str], Dict[str, int]]:
    """对比两版正文。

    返回 (差异行列表, 统计)。差异行用 `+` / `-` 前缀，和 unified diff 一致。
    """
    a = normalize_for_diff(old)
    b = normalize_for_diff(new)
    stats = {"added": 0, "removed": 0, "old_lines": len(a), "new_lines": len(b)}
    if not a and not b:
        return [], stats
    if a == b:
        return [], stats

    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    out: List[str] = []
    last_emitted = -10
    truncated = False

    def emit(prefix: str, line: str) -> bool:
        """追加一行；返回 False 表示已经到上限，该收尾了。"""
        if len(out) >= max_lines:
            return False
        out.append(prefix + line)
        return True

    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        # 分隔远距离的改动块
        if out and i1 - last_emitted > context * 2 + 1:
            if not emit("  ", "…"):
                truncated = True
                break
        last_emitted = i2
        # 逐行判断上限 —— 一个巨大的 replace 块也要能截断，
        # 否则「整篇都改了」的情况会把几千行全吐出来
        for line in a[i1:i2]:
            if not emit("- ", line):
                truncated = True
                break
            stats["removed"] += 1
        if truncated:
            break
        for line in b[j1:j2]:
            if not emit("+ ", line):
                truncated = True
                break
            stats["added"] += 1
        if truncated:
            break

    if truncated:
        out.append("…（差异过多，已截断到 %d 行）" % max_lines)
    return out, stats


def compare(out_dir: str, url: str, new_text: str) -> Dict:
    """把新抓到的正文和上一次比较，返回一份完整结果。"""
    hist_key = key_for(url)
    entry = history_for(out_dir, url)
    versions = entry.get("versions") or []
    new_hash = digest_of(new_text)

    if not versions:
        return {"status": "new", "url": url, "diff": [], "stats": {},
                "message": "这是第一次抓取，已记录为基线版本。"}

    prev_hash = versions[-1].get("hash") or ""
    if prev_hash == new_hash:
        return {"status": "same", "url": url, "diff": [], "stats": {},
                "since": versions[-1].get("captured", ""),
                "message": "内容没有变化。"}

    old_text = read_version_text(out_dir, prev_hash)
    if not old_text:
        return {"status": "new", "url": url, "diff": [], "stats": {},
                "message": "上一次的正文没留下来，无法比较，已更新基线。"}

    diff, stats = diff_texts(old_text, new_text)
    return {
        "status": "changed", "url": url, "diff": diff, "stats": stats,
        "since": versions[-1].get("captured", ""),
        "versions": len(versions),
        "message": "内容有变化。",
    }
