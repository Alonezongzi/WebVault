"""主流水线 —— 把 fetch → extract → inline → render 串起来。

对外只需调用 vault() 一个函数。
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

from .extract import ExtractResult, extract, refresh
from .fetch import FetchResult, fetch
from .inline import AssetStats, InlineOptions, Inliner
from .parse import Node, get_meta_map, parse
from .render import (
    RenderResult, build_data, collect_stylesheet_urls, render, render_full, render_reader,
)
from .util import (
    ensure_dir, escape, human_size, normalize_text, safe_urljoin, slugify, url_host,
)

MODES = ("reader", "full", "data")

ProgressFn = Optional[Callable[[str], None]]


@dataclass
class VaultResult:
    """一次保存的完整结果。"""

    url: str = ""
    final_url: str = ""
    mode: str = "reader"
    title: str = ""
    html: str = ""
    data: Optional[Dict] = None
    ok: bool = False
    error: str = ""
    fetch: Optional[FetchResult] = None
    extraction: Optional[ExtractResult] = None
    stats: Optional[AssetStats] = None
    render_result: Optional[RenderResult] = None
    elapsed: float = 0.0
    out_path: str = ""
    # 抓到的是拦截页时，这里写明是什么拦截（Cloudflare / 登录墙 / 验证码…）
    challenge: str = ""
    challenge_advice: str = ""
    # 上游返回 4xx/5xx 时记下来：存是能存，但存的是错误页
    http_status: int = 0
    http_warning: str = ""
    # 正文太少 / 提取没把握：多半是需要 JS 渲染的页面，或内容不在 HTML 里
    thin_content: bool = False
    thin_advice: str = ""
    # 变更追踪：和上一次保存相比，内容有没有变
    changed: bool = False
    change_stats: Dict[str, int] = field(default_factory=dict)
    first_seen: bool = False
    history_key: str = ""

    def size(self) -> int:
        if self.data is not None and not self.html:
            import json
            return len(json.dumps(self.data, ensure_ascii=False).encode("utf-8"))
        return len(self.html.encode("utf-8"))

    def size_h(self) -> str:
        return human_size(self.size())

    def failed_urls(self) -> List[str]:
        """没能内联成功的资源地址（产物里会留下指向原站的外链）。"""
        if not self.stats:
            return []
        return list(getattr(self.stats, "errors", []) or [])

    def summary(self) -> str:
        if not self.ok:
            return "失败: %s" % self.error
        bits = ["%s" % self.mode, self.title[:40] or "(无标题)", self.size_h()]
        if self.extraction:
            bits.append("%d 字" % self.extraction.word_count)
        if self.stats:
            bits.append("内联 %d" % self.stats.inlined)
            if self.stats.failed:
                # 失败必须显式说出来，否则用户以为归档是完整的
                bits.append("资源失败 %d" % self.stats.failed)
        if self.challenge:
            bits.append("⚠ %s" % self.challenge)
        if self.http_warning:
            bits.append("⚠ HTTP %s" % self.http_status)
        if self.thin_content:
            bits.append("⚠ 正文过少")
        return " | ".join(bits)


def _safe_filename(title: str, url: str, ext: str = ".html") -> str:
    """从标题生成文件名，标题不可用时退回域名+时间。"""
    base = slugify(title, max_len=70) if title else ""
    if not base or base == "untitled":
        host = url_host(url) or "page"
        base = slugify(host, max_len=50)
    name = base + ext
    return name


def unique_path(directory: str, filename: str) -> str:
    """避免覆盖：同名时加 -1 -2 后缀。"""
    path = os.path.join(directory, filename)
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(filename)
    for i in range(1, 1000):
        cand = os.path.join(directory, "%s-%d%s" % (stem, i, ext))
        if not os.path.exists(cand):
            return cand
    return os.path.join(directory, "%s-%d%s" % (stem, int(time.time()), ext))


def vault(
    url: str,
    *,
    mode: str = "reader",
    inline_options: Optional[InlineOptions] = None,
    out_dir: str = "",
    filename: str = "",
    progress: ProgressFn = None,
    timeout: float = 25.0,
    write: bool = True,
    keep_site_css: bool = False,
    show_banner: bool = True,
    extra_headers: Optional[Dict[str, str]] = None,
    verify_tls: bool = False,
    track_changes: bool = True,
) -> VaultResult:
    """核心流程：抓取 → 提取 → 内联 → 渲染 → 落盘。

    永不抛异常；失败信息在结果的 error 与 ok=False 上。

    verify_tls: 是否严格校验 TLS 证书。默认 False —— 很多公开站点证书链
    不完整，严格校验会让工具在正常网页上失败。需要安全性时打开。
    """
    started = time.time()
    res = VaultResult(url=url, mode=mode)

    def say(msg: str) -> None:
        if progress:
            try:
                progress(msg)
            except Exception:
                pass

    if not url or not url.strip():
        res.error = "空 URL"
        return res

    url = url.strip()
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://", url):
        url = "https://" + url
        res.url = url

    mode = (mode or "reader").lower()
    if mode not in MODES:
        mode = "reader"
    res.mode = mode

    # ---------- 1. 抓取 ----------
    say("抓取 %s" % url)
    fr = fetch(url, timeout=timeout, retries=2, headers=extra_headers,
               insecure=not verify_tls)
    res.fetch = fr
    res.final_url = fr.final_url or url

    if not fr.ok and not fr.body:
        res.error = fr.error or "抓取失败"
        res.elapsed = time.time() - started
        return res

    if not fr.body:
        res.error = "服务器返回空内容"
        res.elapsed = time.time() - started
        return res

    say("已抓取 %s（HTTP %s，%s）" % (fr.final_url or url, fr.status, fr.size_h()))
    if fr.error:
        # 有 body 但有错误（比如 403 页面）—— 继续尝试，但记录
        say("警告：%s" % fr.error)

    # 4xx/5xx：页面照样存得下来，但存的是"服务器说没有/出错了"那一页。
    # 不提醒的话，用户会以为抓到了正文 —— 404 页面看起来和真页面一样。
    if fr.status and fr.status >= 400:
        res.http_status = fr.status
        res.http_warning = "服务器返回 HTTP %s，这个网址可能已经失效或无权访问" % fr.status
        say("⚠ %s（存的是服务器的错误页，不是正文）" % res.http_warning)

    html_text = fr.text
    base_url = fr.final_url or url

    # 拦截页检测：抓到 Cloudflare 挑战页 / 登录墙时，产物只会是个空壳。
    # 与其让用户对着一个 20 字的结果发愣，不如直接告诉他被什么拦住了。
    from .fetch import detect_challenge
    chal_reason, chal_advice = detect_challenge(html_text, fr.status)
    if chal_reason:
        res.challenge = chal_reason
        res.challenge_advice = chal_advice
        say("⚠ 看起来是「%s」：%s" % (chal_reason, chal_advice))

    # ---------- 2. 解析 + 提取 ----------
    say("解析并提取正文…")
    try:
        root = parse(html_text)
    except Exception as e:
        res.error = "解析失败: %s" % e
        res.elapsed = time.time() - started
        return res

    # reader/data 模式才需要正文提取；full 模式保留整页
    if mode in ("reader", "data"):
        try:
            ex = extract(html_text, base_url, keep_styles=keep_site_css)
        except Exception as e:
            ex = ExtractResult(title="", html="", text="", method="error")
            say("提取异常，退回整页：%s" % e)
        # 提取完全失败 → 退回 body
        if not ex.html or ex.word_count < 20:
            body = root.find("body")
            if body is not None:
                ex.root = body
                ex.html = body.to_html()
                ex.text = normalize_text(body.text_content())
                ex.word_count = len(ex.text)
                ex.method = "fallback"
        res.extraction = ex
        work_root = ex.root if ex.root is not None else root
    else:
        # full 模式：不需要正文提取，但把 meta 收一下用于统计
        meta = get_meta_map(root)
        ex = ExtractResult(
            title=(meta.get("og:title") or "").strip(),
            site_name=meta.get("og:site_name") or url_host(base_url),
            method="full",
        )
        if not ex.title:
            t = root.find("title")
            if t is not None:
                ex.title = normalize_text(t.text_content())
        res.extraction = ex
        work_root = root

    res.title = ex.title or url_host(base_url) or "untitled"

    # 正文太少时提醒：多半是需要 JS 渲染的 SPA 错误页 / 空壳页。
    # confidence=0 且走了 fallback，基本都是"提取器在整页里找不到任何像正文的东西"。
    if mode == "reader":
        if ex.confidence <= 0.0 and ex.method == "fallback":
            res.thin_content = True
            res.thin_advice = ("这一页几乎提取不到正文（可能是需要 JavaScript 渲染的页面，"
                               "或者这个网址实际跳到了一个错误页）。")
        elif ex.word_count < 120 and len(html_text) > 5000:
            # 页面不小但正文极少 —— 同样是 JS 渲染的典型症状
            res.thin_content = True
            res.thin_advice = "这一页正文只有 %d 字，而原始 HTML 有 %s，内容很可能是靠 JavaScript 渲染的。" % (
                ex.word_count, human_size(len(html_text.encode("utf-8", "replace"))))
        if res.thin_content:
            say("⚠ 正文过少：%s" % res.thin_advice)

    # ---------- 3. 内联资源 ----------
    opts = inline_options or InlineOptions()
    # 阅读模式不需要外部脚本；默认丢弃以免产物里留外链
    if mode == "reader" and (not inline_options or inline_options.scripts == "keep"):
        if inline_options is None:
            opts = InlineOptions(scripts="drop")
    say("内联资源（图片/CSS/字体）…")
    inliner = Inliner(opts, base_url, progress=say)
    try:
        inliner.inline_dom(work_root, base_url)
    except Exception as e:
        say("内联过程中出错（已忽略）：%s" % e)
    res.stats = inliner.stats
    say("资源：%s" % inliner.stats.summary())

    # 内联改的是 DOM，同步回派生字段，否则 html/text/图片数都是旧快照
    if mode in ("reader", "data") and ex.root is not None:
        refresh(ex)

    # 「保留原站样式」时，被摘掉的外链样式表要补抓回来。
    # 它们在 strip_junk 阶段已经离开 DOM，内联阶段看不到，必须单独取。
    if keep_site_css and mode == "reader":
        try:
            urls = collect_stylesheet_urls(ex, base_url)
            for su in urls:
                css_data = inliner._get(su)
                if not css_data:
                    continue
                try:
                    css_text = css_data.decode("utf-8", "replace")
                except Exception:
                    continue
                # 走一遍重写，把里面的图片/字体也内联
                ex.styles.setdefault("inline", []).append(
                    inliner._rewrite_css(css_text, su)
                )
                say("补抓样式表 %s" % su[:80])
        except Exception as e:
            say("补抓样式表失败（已忽略）：%s" % e)

    # ---------- 4. 渲染 ----------
    captured = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    size_line = "%s · 正文 %d 字" % (inliner.stats.summary(), ex.word_count)

    if mode == "data":
        d = build_data(ex, base_url, captured=captured)
        d["requested_url"] = url
        d["inline_stats"] = {
            "fetched": inliner.stats.fetched,
            "inlined": inliner.stats.inlined,
            "skipped": inliner.stats.skipped,
            "failed": inliner.stats.failed,
        }
        res.data = d
        ext = ".json"
    else:
        if mode == "full":
            body_html = render_full(work_root, base_url, captured=captured,
                                    show_banner=show_banner, size_line=size_line)
        else:
            body_html = render_reader(ex, base_url, captured=captured,
                                      stats_line=size_line, keep_site_css=keep_site_css)
        res.html = body_html
        ext = ".html"

    res.ok = True

    # ---------- 5. 落盘 ----------
    if write and out_dir:
        ensure_dir(out_dir)
        name = filename or _safe_filename(res.title, base_url, ext)
        path = unique_path(out_dir, name)
        try:
            if mode == "data":
                import json
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(res.data, f, ensure_ascii=False, indent=2)
            else:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(res.html)
            res.out_path = path
            say("已保存 %s（%s）" % (path, human_size(os.path.getsize(path))))
        except Exception as e:
            res.error = "写文件失败: %s" % e
            res.ok = False

        # ---------- 6. 变更追踪 ----------
        # 只有正文型结果才值得记录（full 模式没有提取正文，没得比）。
        if track_changes and res.ok and mode in ("reader", "data"):
            try:
                from .extract import block_text
                from .watch import compare, key_for, last_text, record
                text = block_text(res.extraction.root) if res.extraction else ""
                if not text and res.extraction:
                    text = res.extraction.text
                if not text and res.data:
                    text = str(res.data.get("text") or "")
                if text:
                    before = last_text(out_dir, url)
                    prev = record(out_dir, url, res.title, text, res.out_path)
                    if prev is not None and before:
                        info = compare(out_dir, url, text)
                        res.changed = info.get("status") == "changed"
                        res.change_stats = info.get("stats") or {}
                        if res.changed:
                            say("内容有变化：+%d / -%d 行" % (
                                res.change_stats.get("added", 0),
                                res.change_stats.get("removed", 0)))
                        else:
                            say("内容和上次一致")
                    res.first_seen = prev is None
                    res.history_key = key_for(url)
            except Exception as e:
                # 变更追踪失败不该影响保存结果
                say("变更追踪跳过：%s" % e)

    res.elapsed = time.time() - started
    return res


# --------------------------------------------------------------------------
# 批量
# --------------------------------------------------------------------------

@dataclass
class BatchItem:
    url: str
    ok: bool = False
    title: str = ""
    path: str = ""
    size: int = 0
    error: str = ""
    elapsed: float = 0.0
    mode: str = ""
    # 资源统计带出来，否则批量时"哪些图没内联成功"完全看不见
    stats: Optional[Any] = None
    failed_assets: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "url": self.url, "ok": self.ok, "title": self.title,
            "path": self.path, "size": self.size, "error": self.error,
            "elapsed": round(self.elapsed, 2), "mode": self.mode,
            "failed_assets": self.failed_assets[:20],
        }


def vault_many(
    urls: List[str],
    *,
    out_dir: str,
    mode: str = "reader",
    workers: int = 4,
    progress: ProgressFn = None,
    inline_options: Optional[InlineOptions] = None,
    timeout: float = 25.0,
    keep_site_css: bool = False,
    show_banner: bool = True,
) -> List[BatchItem]:
    """并发保存多个 URL。"""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    results: List[BatchItem] = []
    urls = [u.strip() for u in urls if u and u.strip() and not u.strip().startswith("#")]
    if not urls:
        return results

    def work(u: str) -> BatchItem:
        r = vault(u, mode=mode, out_dir=out_dir, inline_options=inline_options,
                  timeout=timeout, write=True, keep_site_css=keep_site_css,
                  show_banner=show_banner)
        it = BatchItem(url=u, ok=r.ok, title=r.title, path=r.out_path,
                       size=r.size(), error=r.error, elapsed=r.elapsed,
                       mode=mode, stats=r.stats,
                       failed_assets=r.failed_urls()[:20])
        if progress:
            try:
                progress("[%s] %s" % ("OK" if r.ok else "FAIL", r.summary()))
            except Exception:
                pass
        return it

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futs = {pool.submit(work, u): u for u in urls}
        for f in as_completed(futs):
            try:
                results.append(f.result())
            except Exception as e:
                results.append(BatchItem(url=futs[f], error=str(e)))
    return results
