"""资源内联 —— 把外部依赖全部塞进 HTML，产物变成真·单文件。

处理的东西：
  - <img src>            → data URI（可选择性降采样/压缩）
  - <img srcset>         → 挑最合适的一档，重写
  - CSS <link>           → 抓取后 inline 成 <style>
  - CSS 里的 url(...)    → 递归抓取，转 data URI 或保留（可配）
  - <style> 里的 url(...) → 同上
  - @import              → 递归展开
  - <script src>         → 视策略：保留链接 / 内联 / 丢弃
  - <link rel=icon>      → 内联
  - 字体 (woff/woff2/ttf/otf)
  - <source srcset>, <video poster>
  - background-image 行内样式

体积控制：
  - 总预算 max_total_bytes，超预算时按「优先级低 → 高」逐步放弃
  - 图片可重编码为 JPEG（Pillow 在时）或直接跳过超大的图
  - 每个资源单独 max_asset_bytes 上限

并发：用 ThreadPoolExecutor，I/O 密集，16 线程足够。
"""

from __future__ import annotations

import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Set, Tuple

from .fetch import fetch, guess_mime, guess_mime_from_url
from .parse import Node, VOID_ELEMENTS
from .util import (
    data_uri, escape, human_size, is_fetchable, normalize_url_for_dedup,
    safe_urljoin, sha1_short, url_host,
)

# CSS url(...) 匹配。要处理 url("x") url('x') url(x) 三种
CSS_URL_RE = re.compile(
    r"""url\(\s*(?P<q>["']?)(?P<url>[^)"']+?)(?P=q)\s*\)""", re.I
)
CSS_IMPORT_RE = re.compile(
    r"""@import\s+(?:url\(\s*(?P<q1>["']?)(?P<u1>[^)"']+?)(?P=q1)\s*\)|(?P<q2>["'])(?P<u2>[^"']+?)(?P=q2))\s*(?P<media>[^;]*);""",
    re.I,
)
CSS_CHARSET_RE = re.compile(r"""@charset\s+["'][^"']*["']\s*;""", re.I)

# 可以内联的数据型 MIME
INLINE_MIME_PREFIXES = ("image/", "font/", "application/font", "application/x-font")
TEXTUAL_MIME = ("text/css", "application/json", "image/svg+xml", "text/plain")

# srcset 解析
SRCSET_RE = re.compile(r"\s*([^\s,]+)\s*(?:(\d+(?:\.\d+)?)([wx]))?\s*(?:,|$)")


def _base64_size(n: int) -> int:
    """原始字节数变成 base64 data URI 之后的大致字节数。

    base64 是 3 字节 → 4 字符，再加 `data:<mime>;base64,` 前缀。
    用它来扣预算，产物才不会超上限。
    """
    if n <= 0:
        return 0
    return ((n + 2) // 3) * 4 + 48


def _is_font(mime: str, url: str) -> bool:
    """判断一个资源是不是字体文件。"""
    m = (mime or "").lower()
    if m.startswith("font/") or "font" in m:
        return True
    path = (url or "").split("?", 1)[0].split("#", 1)[0].lower()
    return path.endswith((".woff", ".woff2", ".ttf", ".otf", ".eot"))


@dataclass
class AssetStats:
    """内联过程的统计。"""

    fetched: int = 0
    inlined: int = 0
    skipped: int = 0
    failed: int = 0
    bytes_in: int = 0
    bytes_out: int = 0
    inline_bytes: int = 0
    budget_spent: int = 0
    budget_total: int = 0
    by_kind: Dict[str, int] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)
    skipped_urls: List[str] = field(default_factory=list)

    def note(self, kind: str, n: int = 1) -> None:
        self.by_kind[kind] = self.by_kind.get(kind, 0) + n

    def summary(self) -> str:
        base = (
            "抓取 %d / 内联 %d / 跳过 %d / 失败 %d；原始 %s → 内联 %s"
            % (self.fetched, self.inlined, self.skipped, self.failed,
               human_size(self.bytes_in), human_size(self.inline_bytes))
        )
        if self.budget_total:
            pct = 100.0 * self.budget_spent / float(self.budget_total)
            base += "；预算 %s/%s (%.0f%%)" % (
                human_size(self.budget_spent), human_size(self.budget_total), pct)
        return base


@dataclass
class InlineOptions:
    images: bool = True
    css: bool = True
    fonts: bool = True
    scripts: str = "keep"          # keep | inline | drop
    max_asset_bytes: int = 4 * 1024 * 1024
    max_total_bytes: int = 24 * 1024 * 1024
    workers: int = 8
    timeout: float = 20.0
    compress_images: bool = True
    image_max_width: int = 1600
    jpeg_quality: int = 82
    keep_original_urls: bool = True   # 在 data URI 旁保留原地址注释
    allow_external: bool = True       # 允许抓非当前站的资源
    verify_tls: bool = False          # 是否严格校验 TLS 证书


class Inliner:
    """资源内联器。线程安全（缓存加了锁）。"""

    def __init__(self, options: Optional[InlineOptions] = None,
                 base_url: str = "", progress: Optional[Callable[[str], None]] = None):
        self.opt = options or InlineOptions()
        self.base_url = base_url
        self.stats = AssetStats()
        self.stats.budget_total = self.opt.max_total_bytes
        self.progress = progress
        self._cache: Dict[str, Optional[bytes]] = {}
        self._mime_cache: Dict[str, str] = {}
        self._lock = threading.Lock()
        self._budget_left = self.opt.max_total_bytes
        self._budget_lock = threading.Lock()
        # 自适应限速：整个线程池共享一个"暂停到某个时刻"的时间戳。
        # 收到 429 就让所有人一起慢下来，而不是各自重试再一起撞墙。
        self._throttle_until = 0.0
        self._throttle_level = 0
        self._throttle_lock = threading.Lock()

    # ------------------------------------------------------------------
    # 自适应限速
    # ------------------------------------------------------------------
    def _throttle_wait(self) -> None:
        """如果当前处于退避窗口，先等一等再发请求。"""
        with self._throttle_lock:
            wait = self._throttle_until - time.time()
        if wait > 0:
            time.sleep(min(wait, 5.0))

    def _note_status(self, status: int) -> None:
        """根据响应状态调整全局退避。

        429 表示"你太快了"——这时候继续并发只会拿到更多 429。
        退避窗口按 2 的幂增长（0.5s → 1s → 2s …），成功一次就降一级。
        """
        if status == 429:
            with self._throttle_lock:
                self._throttle_level = min(self._throttle_level + 1, 5)
                delay = 0.5 * (2 ** (self._throttle_level - 1))
                self._throttle_until = max(self._throttle_until, time.time() + delay)
        elif 200 <= status < 400:
            with self._throttle_lock:
                if self._throttle_level > 0:
                    self._throttle_level -= 1

    # ------------------------------------------------------------------
    # 预算
    # ------------------------------------------------------------------
    def _take_budget(self, n: int) -> bool:
        """按 data URI 的最终体积扣预算。

        关键：预算要按 base64 之后的体积算。base64 会膨胀约 4/3，
        如果按原始字节扣，产物会比用户设定的上限大三分之一。
        """
        cost = _base64_size(n)
        with self._budget_lock:
            if cost > self._budget_left:
                return False
            self._budget_left -= cost
            self.stats.budget_spent += cost
            return True

    def _give_budget(self, n: int) -> None:
        cost = _base64_size(n)
        with self._budget_lock:
            self._budget_left += cost
            self.stats.budget_spent = max(0, self.stats.budget_spent - cost)

    def budget_left(self) -> int:
        with self._budget_lock:
            return self._budget_left

    # ------------------------------------------------------------------
    # 抓取（带缓存）
    # ------------------------------------------------------------------
    def _get(self, url: str) -> Optional[bytes]:
        key = normalize_url_for_dedup(url) or url
        with self._lock:
            if key in self._cache:
                return self._cache[key]

        data = None
        mime = ""
        try:
            # 先看是否处于限流退避窗口（别的线程刚被 429 过）
            self._throttle_wait()
            r = fetch(url, timeout=self.opt.timeout, retries=2, max_bytes=self.opt.max_asset_bytes,
                      referer=self.base_url, insecure=not self.opt.verify_tls)
            self._note_status(r.status or 0)
            self._throttle_wait()
            if r.ok and r.body:
                data = r.body
                mime = guess_mime(data, r.final_url or url, r.content_type)
            elif r.body and r.status in (403, 404):
                data = None
            if not data:
                with self._lock:
                    if len(self.stats.errors) < 40:
                        self.stats.errors.append(
                            "%s (%s)" % (url[:100], (r.error or "空响应")[:60]))
        except Exception as e:
            with self._lock:
                if len(self.stats.errors) < 40:
                    self.stats.errors.append("%s: %s" % (url[:80], str(e)[:80]))
            data = None

        with self._lock:
            self._cache[key] = data
            if mime:
                self._mime_cache[key] = mime
            self.stats.fetched += 1
            if data:
                self.stats.bytes_in += len(data)
            else:
                self.stats.failed += 1
        return data

    def _mime_for(self, url: str, data: bytes = b"") -> str:
        key = normalize_url_for_dedup(url) or url
        with self._lock:
            if key in self._mime_cache:
                return self._mime_cache[key]
        return guess_mime(data, url)

    # ------------------------------------------------------------------
    # 图片处理
    # ------------------------------------------------------------------
    def _process_image(self, data: bytes, mime: str) -> Tuple[bytes, str]:
        """按需压缩图片。Pillow 不在就原样返回。"""
        if not self.opt.compress_images:
            return data, mime
        if mime in ("image/svg+xml", "image/gif", "image/webp"):
            return data, mime
        if len(data) < 24 * 1024:
            return data, mime
        try:
            from PIL import Image
            import io as _io
        except Exception:
            return data, mime
        try:
            im = Image.open(_io.BytesIO(data))
            im.load()
            changed = False
            if im.width > self.opt.image_max_width:
                ratio = self.opt.image_max_width / float(im.width)
                im = im.resize((self.opt.image_max_width, max(1, int(im.height * ratio))),
                               Image.LANCZOS)
                changed = True
            # 有透明通道保留 PNG，否则转 JPEG
            has_alpha = im.mode in ("RGBA", "LA", "P") and (
                "transparency" in im.info or im.mode in ("RGBA", "LA"))
            out = _io.BytesIO()
            if has_alpha:
                if changed:
                    im.save(out, format="PNG", optimize=True)
                    new = out.getvalue()
                    if len(new) < len(data):
                        return new, "image/png"
                return data, mime
            if im.mode not in ("RGB", "L"):
                im = im.convert("RGB")
            im.save(out, format="JPEG", quality=self.opt.jpeg_quality,
                    optimize=True, progressive=True)
            new = out.getvalue()
            if len(new) < len(data):
                return new, "image/jpeg"
        except Exception:
            pass
        return data, mime

    # ------------------------------------------------------------------
    # CSS 处理
    # ------------------------------------------------------------------
    def _rewrite_css(self, css: str, css_url: str, depth: int = 0) -> str:
        """把一个 CSS 文本里的 url() 全部内联，@import 递归展开。"""
        if not css:
            return ""
        if depth > 4:
            return css

        # 1) 展开 @import（就地替换为被导入的内容）
        def repl_import(m):
            target = m.group("u1") or m.group("u2") or ""
            if not target or not is_fetchable(safe_urljoin(css_url, target)):
                return m.group(0)
            absu = safe_urljoin(css_url, target)
            if not self._url_allowed(absu):
                return ""
            data = self._get(absu)
            if not data:
                return ""
            try:
                text = data.decode("utf-8", "replace")
            except Exception:
                return ""
            inner = self._rewrite_css(text, absu, depth + 1)
            self.stats.note("css-import")
            media = (m.group("media") or "").strip()
            if media:
                return "@media %s{%s}" % (media, inner)
            return inner

        css = CSS_IMPORT_RE.sub(repl_import, css)
        css = CSS_CHARSET_RE.sub("", css)

        # 2) 内联 url(...)
        def repl_url(m):
            raw = (m.group("url") or "").strip()
            if not raw or raw.startswith("data:"):
                return m.group(0)
            if raw.startswith("#"):
                return m.group(0)
            absu = safe_urljoin(css_url, raw)
            if not is_fetchable(absu) or not self._url_allowed(absu):
                return m.group(0)
            uri = self._inline_one(absu, kind="css-asset")
            if uri:
                return 'url("%s")' % uri
            # 失败：改成绝对地址，至少不会 404
            return 'url("%s")' % absu

        return CSS_URL_RE.sub(repl_url, css)

    def _url_allowed(self, url: str) -> bool:
        if self.opt.allow_external:
            return True
        return url_host(url) == url_host(self.base_url)

    # ------------------------------------------------------------------
    # 单个资源 → data URI
    # ------------------------------------------------------------------
    def _inline_one(self, url: str, kind: str = "asset") -> str:
        if not url or url.startswith("data:"):
            return url or ""
        data = self._get(url)
        if not data:
            with self._lock:
                if len(self.stats.skipped_urls) < 60:
                    self.stats.skipped_urls.append(url[:120])
            self.stats.skipped += 1
            return ""
        if len(data) > self.opt.max_asset_bytes:
            self.stats.skipped += 1
            with self._lock:
                if len(self.stats.skipped_urls) < 60:
                    self.stats.skipped_urls.append("(too big) " + url[:110])
            return ""

        mime = self._mime_for(url, data)

        # 字体开关：--no-fonts 时字体不内联，但保留原始绝对地址（不是坏链）
        if not self.opt.fonts and _is_font(mime, url):
            self.stats.skipped += 1
            with self._lock:
                if len(self.stats.skipped_urls) < 60:
                    self.stats.skipped_urls.append("(font skipped) " + url[:100])
            return ""

        if kind == "img":
            data, mime = self._process_image(data, mime)

        if not self._take_budget(len(data)):
            self.stats.skipped += 1
            with self._lock:
                if len(self.stats.skipped_urls) < 60:
                    self.stats.skipped_urls.append("(budget) " + url[:110])
            return ""

        uri = data_uri(data, mime)
        with self._lock:
            self.stats.inlined += 1
            self.stats.inline_bytes += len(uri)
            self.stats.note(kind)
        return uri

    # ------------------------------------------------------------------
    # srcset
    # ------------------------------------------------------------------
    def _pick_srcset(self, value: str, base: str) -> str:
        """从 srcset 里挑最大的那一档，返回内联后的单个 URL。"""
        best_url = ""
        best_score = -1.0
        for m in SRCSET_RE.finditer(value or ""):
            u = m.group(1)
            if not u:
                continue
            num = m.group(2)
            unit = m.group(3) or "w"
            score = 1.0
            if num:
                try:
                    score = float(num)
                    if unit == "x":
                        score *= 1000
                except Exception:
                    score = 1.0
            if score > best_score:
                best_score = score
                best_url = u
        if not best_url:
            return ""
        return safe_urljoin(base, best_url)

    # ------------------------------------------------------------------
    # 主流程：处理 DOM
    # ------------------------------------------------------------------
    def inline_dom(self, root: Node, base_url: str = "") -> Node:
        """就地内联整棵树的资源。返回同一个 root 便于链式调用。"""
        base = base_url or self.base_url
        opt = self.opt

        jobs: List[Tuple[Node, str]] = []   # (元素, 资源URL) —— 图片类，可并发预抓

        # ---- 收集需要处理的 URL ----
        for n in root.iter():
            if not n.is_element():
                continue
            tag = n.tag

            if tag == "img" and opt.images:
                src = n.get("src") or n.get("data-src") or n.get("data-original") or \
                      n.get("data-lazy-src") or n.get("data-echo")
                srcset = n.get("srcset") or n.get("data-srcset")
                if not src and srcset:
                    src = self._pick_srcset(srcset, base)
                elif srcset:
                    # 有 srcset 就优先用它挑的大图
                    picked = self._pick_srcset(srcset, base)
                    if picked:
                        src = picked
                if src:
                    absu = safe_urljoin(base, src)
                    if is_fetchable(absu) and self._url_allowed(absu):
                        jobs.append((n, absu))
                # 清理懒加载属性，避免产物里留一堆 data-src
                for attr in ("data-src", "data-original", "data-lazy-src",
                             "data-echo", "data-srcset", "loading"):
                    n.drop(attr)
                n.drop("srcset")

            elif tag == "source" and opt.images:
                srcset = n.get("srcset") or n.get("data-srcset")
                if srcset:
                    picked = self._pick_srcset(srcset, base)
                    if picked:
                        absu = safe_urljoin(base, picked)
                        if is_fetchable(absu) and self._url_allowed(absu):
                            jobs.append((n, absu))
                    n.drop("srcset")

            elif tag in ("video", "audio") and n.has("poster"):
                absu = safe_urljoin(base, n.get("poster"))
                if is_fetchable(absu) and self._url_allowed(absu):
                    jobs.append((n, absu))

            elif tag == "link" and opt.css:
                rel = (n.get("rel") or "").lower()
                href = n.get("href") or ""
                if "stylesheet" in rel and href:
                    absu = safe_urljoin(base, href)
                    if is_fetchable(absu) and self._url_allowed(absu):
                        jobs.append((n, absu))
                elif "icon" in rel and href and opt.images:
                    absu = safe_urljoin(base, href)
                    if is_fetchable(absu) and self._url_allowed(absu):
                        jobs.append((n, absu))

            elif tag == "style":
                # <style> 里的 url() 也要重写（稍后统一处理）
                pass

        # ---- 并发预抓 ----
        if opt.workers > 1 and len(jobs) > 1:
            uniq = []
            seen = set()
            for _, u in jobs:
                k = normalize_url_for_dedup(u) or u
                if k not in seen:
                    seen.add(k)
                    uniq.append(u)
            if self.progress:
                self.progress("预抓取 %d 个资源…" % len(uniq))
            with ThreadPoolExecutor(max_workers=max(1, opt.workers)) as pool:
                futs = {pool.submit(self._get, u): u for u in uniq}
                for f in as_completed(futs):
                    try:
                        f.result()
                    except Exception:
                        pass

        # ---- 就地替换 ----
        for n, absu in jobs:
            tag = n.tag
            if tag == "link":
                rel = (n.get("rel") or "").lower()
                if "stylesheet" in rel:
                    data = self._get(absu)
                    if data:
                        try:
                            css_text = data.decode("utf-8", "replace")
                        except Exception:
                            css_text = ""
                        rewritten = self._rewrite_css(css_text, absu)
                        self.stats.note("css")
                        # 把 <link rel=stylesheet> 换成 <style>
                        node = Node("style")
                        tn = Node(kind="text")
                        tn.text = rewritten
                        node.append(tn)
                        self._replace_node(n, node)
                    else:
                        n.set("href", absu)
                elif "icon" in rel:
                    uri = self._inline_one(absu, kind="icon")
                    if uri:
                        n.set("href", uri)
                    else:
                        n.set("href", absu)

            elif tag in ("img", "source"):
                uri = self._inline_one(absu, kind="img")
                if uri:
                    n.set("src", uri)
                else:
                    n.set("src", absu)

            elif tag in ("video", "audio"):
                uri = self._inline_one(absu, kind="media")
                if uri:
                    n.set("poster", uri)

        # ---- 处理 <style> 内容 ----
        if opt.css:
            for n in root.find_all("style"):
                for child in n.children:
                    if child.is_text() and child.text:
                        child.text = self._rewrite_css(child.text, base)

        # ---- 处理行内 style 属性里的 url() ----
        if opt.css:
            for n in root.iter():
                if not n.is_element():
                    continue
                st = n.attrs.get("style")
                if st and "url(" in st.lower():
                    n.attrs["style"] = self._rewrite_css(st, base)

        # ---- script ----
        self._handle_scripts(root, base)

        return root

    def _replace_node(self, old: Node, new: Node) -> None:
        parent = old.parent
        if parent is None:
            return
        try:
            idx = parent.children.index(old)
        except ValueError:
            return
        parent.children[idx] = new
        new.parent = parent
        old.parent = None

    def _handle_scripts(self, root: Node, base: str) -> None:
        mode = (self.opt.scripts or "keep").lower()
        if mode == "keep":
            return
        for n in list(root.find_all("script")):
            src = n.get("src")
            if not src:
                # 内联脚本：内联模式下保留，丢弃模式下删除
                if mode == "drop":
                    n.remove()
                continue
            if mode == "drop":
                n.remove()
                self.stats.note("script-dropped")
                continue
            if mode == "inline":
                absu = safe_urljoin(base, src)
                if is_fetchable(absu) and self._url_allowed(absu):
                    data = self._get(absu)
                    if data:
                        try:
                            js = data.decode("utf-8", "replace")
                        except Exception:
                            js = ""
                        if js and len(js) < self.opt.max_asset_bytes:
                            if not self._take_budget(len(js)):
                                n.set("src", absu)
                                continue
                            for c in list(n.children):
                                c.remove()
                            tn = Node(kind="text")
                            tn.text = js
                            n.append(tn)
                            n.drop("src")
                            self.stats.note("script-inline")
                            continue
                n.set("src", absu)


# --------------------------------------------------------------------------
# 便捷入口
# --------------------------------------------------------------------------

def inline_all(root: Node, base_url: str, options: Optional[InlineOptions] = None,
               progress: Optional[Callable[[str], None]] = None) -> AssetStats:
    inliner = Inliner(options, base_url, progress)
    inliner.inline_dom(root, base_url)
    return inliner.stats
