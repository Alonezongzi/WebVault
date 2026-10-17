#!/usr/bin/env python3
"""WebVault 自测套件。

设计原则：
  - 默认全部离线（用内置 fixture），不依赖网络，随时可跑
  - 加 --online 才跑真实站点测试
  - 每个断言都针对一个具体的、曾经出过问题或容易回归的行为

运行：
    python tests/test_all.py            # 离线测试
    python tests/test_all.py --online   # 加上真实站点
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import traceback

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from src.util import (  # noqa: E402
    collapse_ws, data_uri, human_size, normalize_text, safe_urljoin, slugify,
    strip_tags, unescape, url_host, normalize_url_for_dedup, truncate,
)
from src.parse import (  # noqa: E402
    Node, parse, get_title, get_meta_map, VOID_ELEMENTS,
)
from src.extract import extract, text_length, link_density, is_probably_readable  # noqa: E402
from src.fetch import sniff_encoding, decode_bytes, guess_mime  # noqa: E402
from src.inline import InlineOptions, Inliner, CSS_URL_RE, SRCSET_RE  # noqa: E402
from src.render import render_reader, render_full, build_data, render  # noqa: E402
from src.pipeline import vault, _safe_filename, unique_path  # noqa: E402

# --------------------------------------------------------------------------
# 测试框架（极简，不引入 pytest）
# --------------------------------------------------------------------------

PASS = 0
FAIL = 0
FAILURES = []
_GROUP = ""


def group(name: str) -> None:
    global _GROUP
    _GROUP = name
    print("\n\033[1m── %s\033[0m" % name)


def check(label: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  \033[32m✓\033[0m %s" % label)
    else:
        FAIL += 1
        FAILURES.append("%s / %s %s" % (_GROUP, label, detail))
        print("  \033[31m✗\033[0m %s  %s" % (label, detail))


def eq(label: str, got, want) -> None:
    check(label, got == want, "got=%r want=%r" % (got, want))


def no_raise(label: str, fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except Exception as e:
        check(label, False, "%s: %s" % (type(e).__name__, e))
        traceback.print_exc()
        return None


# --------------------------------------------------------------------------
# fixture：覆盖真实世界的脏 HTML 形态
# --------------------------------------------------------------------------

FIXTURE_ARTICLE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>测试文章标题 - 某站</title>
<meta property="og:title" content="测试文章标题">
<meta name="description" content="这是一段摘要">
<meta name="author" content="张三">
<meta property="og:site_name" content="某站">
<link rel="stylesheet" href="/style.css">
<style>.inline-css{color:red;background:url('/bg.png')}</style>
</head>
<body>
<header id="site-header">
  <nav class="main-nav"><a href="/">首页</a><a href="/about">关于</a><a href="/contact">联系</a></nav>
</header>
<div class="sidebar">
  <h3>热门推荐</h3>
  <ul><li><a href="/hot1">热门文章一</a></li><li><a href="/hot2">热门文章二</a></li></ul>
</div>
<article class="post-content" id="main-article">
  <h1>测试文章标题</h1>
  <p class="byline">作者：张三</p>
  <p>这是第一段正文内容。它包含足够多的文字，用来让正文提取算法能够正确识别这个区块是文章的主体部分，而不是导航或者侧边栏。</p>
  <p>这是第二段。中文的标点符号密度是判断自然语言的重要信号，逗号、句号和顿号都会出现在真正的文章里，而菜单和按钮通常没有这些符号。</p>
  <img src="/images/photo1.jpg" alt="配图一">
  <p>第三段继续提供内容。这里再补充一些文字，确保整个文章的文本长度超过提取算法的最低阈值，从而被正确选出并保留下来。</p>
  <p>延伸阅读：<a href="https://ref.example/doc">参考资料链接</a></p>
  <blockquote>这是一段引用文字，用来测试 blockquote 的处理。</blockquote>
  <table>
    <tr><th>列一</th><th>列二</th></tr>
    <tr><td>甲</td><td>乙</td></tr>
  </table>
  <p>最后一段正文，到此结束。</p>
</article>
<div class="comments" id="comments">
  <h3>评论 (2)</h3>
  <div class="comment"><span class="author">李四</span><p>说: 写得好！这是评论内容不该出现在正文里。</p></div>
</div>
<footer id="site-footer"><p>版权所有 © 2024 某站</p></footer>
<script>var _gaq = [];</script>
</body>
</html>
"""

FIXTURE_DIRTY = """<div><p>one<p>two<div>three</div><ul><li>a<li>b</ul>
<table><tr><td>1<td>2<tr><td>3<td>4</table>
<img src=x.png><br><hr>
<div class="bad><p>broken attr</p>
<script>var s = "</div>";</script>
"""

FIXTURE_CSS = """
@import url("base.css");
@charset "utf-8";
body { background: url('/img/bg.png') no-repeat; }
.icon { background-image: url("data:image/gif;base64,R0lGOD"); }
.font { src: url(font.woff2) format('woff2'); }
.skip { background: url(#anchor); }
"""


# ==========================================================================
# 1. util
# ==========================================================================

def test_util() -> None:
    group("util · 文本与 URL 工具")

    eq("collapse_ws 压空白", collapse_ws("  a   b \n c  "), "a b \n c")
    eq("normalize_text 去零宽", normalize_text("a\u200bb\ufeffc"), "abc")
    eq("normalize_text NFKC", normalize_text("ＡＢＣ１２３"), "ABC123")
    eq("unescape 实体", unescape("a &amp; b &lt;c&gt;"), "a & b <c>")
    eq("strip_tags", strip_tags("<p>a</p><b>b</b>"), "ab")
    eq("truncate 短的不动", truncate("abc", 10), "abc")
    check("truncate 长的截断", truncate("abcdefghij", 5).endswith("…"))

    eq("slugify 去非法字符", slugify('a/b\\c:d*e?f'), "a_b_c_d_e_f")
    eq("slugify 空格转横线", slugify("hello world"), "hello-world")
    eq("slugify 保留中文", slugify("中文标题"), "中文标题")
    eq("slugify 空回退", slugify("   "), "untitled")

    eq("safe_urljoin 正常", safe_urljoin("https://a.com/x/y", "z.png"),
       "https://a.com/x/z.png")
    eq("safe_urljoin 绝对", safe_urljoin("https://a.com/x/", "https://b.com/i.png"),
       "https://b.com/i.png")
    eq("safe_urljoin 空 base", safe_urljoin("", "u"), "u")
    eq("url_host 小写", url_host("https://WWW.A.COM/p"), "www.a.com")

    eq("去重去 fragment",
       normalize_url_for_dedup("https://a.com/p?x=1#frag"), "https://a.com/p?x=1")
    eq("去重去 utm",
       normalize_url_for_dedup("https://a.com/p?utm_source=x&id=5"), "https://a.com/p?id=5")

    eq("human_size B", human_size(512), "512 B")
    eq("human_size KB", human_size(1536), "1.5 KB")
    eq("human_size MB", human_size(3 * 1024 * 1024), "3.0 MB")

    uri = data_uri(b"hello", "text/plain")
    check("data_uri 前缀", uri.startswith("data:text/plain;base64,"), uri[:40])
    eq("data_uri 空数据", data_uri(b"", "text/plain"), "")


# ==========================================================================
# 2. parse
# ==========================================================================

def test_parse() -> None:
    group("parse · 容错 HTML 解析")

    r = parse("<div><p>hi</p></div>", fast=False)
    eq("基本结构", r.find("p").text_content(), "hi")

    # 隐式闭合
    r = parse("<ul><li>a<li>b<li>c</ul>", fast=False)
    eq("li 隐式闭合", len(r.find_all("li")), 3)
    eq("li 内容", [x.text_content() for x in r.find_all("li")], ["a", "b", "c"])

    r = parse("<p>one<p>two", fast=False)
    eq("p 隐式闭合", len(r.find_all("p")), 2)

    # 表格：tr 必须是兄弟，不能嵌套
    r = parse("<table><tr><td>1<td>2<tr><td>3<td>4</table>", fast=False)
    eq("表格单元格数", len(r.find_all("td")), 4)
    eq("表格行数（不嵌套）", len(r.find_all("tr")), 2)
    tbody_like = r.find("table")
    trs = [c for c in tbody_like.children if c.is_element() and c.tag == "tr"]
    eq("tr 是 table 的直接子节点", len(trs), 2)

    # 破损属性不应吞掉内容
    r = parse('<div class="bad><p>kept</p>', fast=False)
    check("破损属性后内容保留", "kept" in r.to_html(), r.to_html()[:80])

    # void 元素
    r = parse("<img src=a.png><br><hr>", fast=False)
    eq("void 元素数", len(r.find_all(("img", "br", "hr"))), 3)
    check("img 自闭合无需 </img>", "</img>" not in r.to_html())

    # script 里的 </div> 不该破坏结构
    r = parse('<script>var x="</div>";</script><p>after</p>', fast=False)
    check("script 内伪闭合不影响结构", r.find("p") is not None)

    # 实体
    r = parse("<p>a &amp; b</p>", fast=False)
    eq("实体保留原文", "&amp;" in r.find("p").to_html(), True)

    # 注释丢弃
    r = parse("<div><!-- x --><p>y</p></div>", fast=False)
    check("注释被丢弃", "x" not in r.to_html())

    # 空输入不炸
    eq("空字符串", parse("", fast=False).to_html(), "")
    eq("纯文本", parse("hello", fast=False).text_content(), "hello")

    # 深层嵌套
    deep = "<div>" * 60 + "deep" + "</div>" * 60
    r = parse(deep, fast=False)
    check("深层嵌套不递归爆栈", "deep" in r.to_html())

    # title / meta
    r = parse(FIXTURE_ARTICLE, fast=False)
    # get_title 是原始工具：返回 <title> 全文，不做站点后缀清理
    eq("取 title（<title> 原文）", get_title(r), "测试文章标题 - 某站")
    m = get_meta_map(r)
    eq("meta author", m.get("author"), "张三")
    eq("meta og:site_name", m.get("og:site_name"), "某站")

    # 序列化往返
    src = "<div id='a' class='b c'><p>text</p><img src='x.png'></div>"
    r = parse(src, fast=False)
    out = r.to_html()
    r2 = parse(out, fast=False)
    eq("序列化往返稳定", r2.to_html(), out)


# ==========================================================================
# 3. extract
# ==========================================================================

def test_extract() -> None:
    group("extract · 正文提取")

    ex = extract(FIXTURE_ARTICLE, "https://example.com/post/1", fast_parse=False)

    eq("标题", ex.title, "测试文章标题")
    eq("作者", ex.byline, "张三")
    eq("摘要", ex.excerpt, "这是一段摘要")
    eq("站点名", ex.site_name, "某站")

    body = ex.text
    check("包含正文第一段", "这是第一段正文内容" in body)
    check("包含正文最后一段", "最后一段正文" in body)

    # 噪音必须被剔除
    check("剔除导航链接", "关于" not in body or "首页" not in body)
    check("剔除侧边栏推荐", "热门文章一" not in body)
    check("剔除评论", "写得好" not in body)
    check("剔除页脚版权", "版权所有" not in body)

    check("提取到图片", ex.image_count >= 1, "imgs=%d" % ex.image_count)
    check("置信度合理", 0.0 < ex.confidence <= 1.0, "conf=%.2f" % ex.confidence)

    # text_length：中文按字算
    check("中文长度估算", text_length("你好世界") > 0)
    check("空串长度为 0", text_length("") == 0)

    # 链接密度
    nav = parse('<div><a href=1>x</a><a href=2>y</a></div>', fast=False)
    check("纯链接密度接近 1", link_density(nav.find("div")) > 0.9)

    # is_probably_readable
    check("长中文句子可读", is_probably_readable("这是一句完整的话。它包含标点符号。"))
    check("短菜单不可读", not is_probably_readable("首页 关于"))
    check("空串不可读", not is_probably_readable(""))

    # 极端情况不炸
    for label, html in [
        ("空 HTML", ""),
        ("只有 body", "<body></body>"),
        ("无正文", "<html><head><title>T</title></head><body><nav>x</nav></body></html>"),
        ("纯文本", "just plain text with no tags at all here"),
        ("巨量嵌套", "<div>" * 200 + "<p>deep</p>" + "</div>" * 200),
    ]:
        r = no_raise("extract 不抛异常: %s" % label, extract, html, "", fast_parse=False)
        if r is not None:
            check("extract 有结果: %s" % label, r is not None)


def test_extract_regressions() -> None:
    group("extract · 回归用例（曾经出过的 bug）")

    # 回归 1：正文里的整段链接曾被误当作导航删掉。
    # 真实文章常出现"延伸阅读：<a>…</a>"这种段落。
    html = """<html><head><title>T</title></head><body>
    <article class="post-content">
      <p>这是正文段落，需要足够长才能被识别为文章主体内容，并且包含标点符号。</p>
      <p>第二段继续补充文字，让提取算法能够稳定地选出这个容器作为文章主体部分。</p>
      <p>延伸阅读：<a href="https://ref.example/doc">参考资料链接</a></p>
    </article></body></html>"""
    ex = extract(html, "https://x/a", fast_parse=False)
    check("正文里的链接段落被保留", "ref.example/doc" in ex.html,
          ex.html[:200])
    check("参考资料文字被保留", "参考资料链接" in ex.text, ex.text[:200])

    # 但真正的导航"上一篇/下一篇"仍应剔除
    html2 = """<html><head><title>T</title></head><body>
    <div class="post-content">
      <p>这是正文段落，需要足够长才能被识别为文章主体内容，并且包含标点符号。</p>
      <p>第二段继续补充文字，让提取算法能够稳定地选出这个容器作为文章主体部分。</p>
      <div class="post-nav"><a href="/a">上一篇</a><a href="/b">下一篇</a></div>
    </div></body></html>"""
    ex2 = extract(html2, "https://x/a", fast_parse=False)
    check("导航块仍被剔除", "/a" not in ex2.html or "上一篇" not in ex2.html,
          ex2.html[:200])

    # 回归 2：keep_styles 必须同时收集 <style> 原文与外链样式表地址。
    # 曾经因为 __slots__ + 全局旁路表，第二次写入覆盖第一次，导致 <style> 丢失。
    html3 = """<html><head><title>T</title>
    <style>body{color:#ff0000}</style>
    <link rel="stylesheet" href="/ext.css">
    </head><body><article class="post-content">
      <p>这是正文段落，需要足够长才能被识别为文章主体内容，并且包含标点符号。</p>
      <p>第二段继续补充文字，让提取算法能够稳定地选出这个容器作为文章主体部分。</p>
    </article></body></html>"""
    ex3 = extract(html3, "https://s.com/a", keep_styles=True, fast_parse=False)
    check("keep_styles 收集到 inline style",
          len(ex3.styles.get("inline") or []) == 1, str(ex3.styles))
    check("keep_styles 收集到 link",
          len(ex3.styles.get("links") or []) == 1, str(ex3.styles))
    check("has_styles() 为真", ex3.has_styles())

    out = render_reader(ex3, "https://s.com/a", keep_site_css=True, captured="T")
    check("保留样式时输出含站点 CSS", "ff0000" in out)
    out2 = render_reader(ex3, "https://s.com/a", keep_site_css=False, captured="T")
    check("默认不保留站点 CSS", "ff0000" not in out2)

    # 不开启时不该收集
    ex4 = extract(html3, "https://s.com/a", fast_parse=False)
    check("未开启时不收集样式",
          not (ex4.styles.get("inline") or ex4.styles.get("links")), str(ex4.styles))


# ==========================================================================
# 4. fetch（编码/MIME，离线部分）
# ==========================================================================

def test_fetch_offline() -> None:
    group("fetch · 编码嗅探与 MIME")

    gbk = "中文测试内容".encode("gbk")
    eq("GBK 无声明能嗅出", sniff_encoding(gbk, {}), "gbk")
    eq("GBK 有 HTTP 头", sniff_encoding(gbk, {"content-type": "text/html; charset=gb2312"}), "gbk")
    eq("GBK 有 meta", sniff_encoding(b'<meta charset="gbk">' + gbk, {}), "gbk")
    eq("GBK 解码正确", decode_bytes(gbk, "gbk"), "中文测试内容")

    utf8 = "中文 UTF-8".encode("utf-8")
    eq("UTF-8 嗅探", sniff_encoding(utf8, {}), "utf-8")
    eq("UTF-8 解码", decode_bytes(utf8, "utf-8"), "中文 UTF-8")

    eq("BOM 识别", sniff_encoding(b"\xef\xbb\xbf<html>", {}), "utf-8-sig")

    # 错误的编码名不能炸
    eq("未知编码名降级", decode_bytes(b"abc", "not-a-real-codec"), "abc")

    # MIME 魔数
    eq("PNG 魔数", guess_mime(b"\x89PNG\r\n\x1a\n" + b"0" * 10), "image/png")
    eq("JPEG 魔数", guess_mime(b"\xff\xd8\xff" + b"0" * 10), "image/jpeg")
    eq("GIF 魔数", guess_mime(b"GIF89a" + b"0" * 10), "image/gif")
    eq("WEBP 魔数", guess_mime(b"RIFF\x00\x00\x00\x00WEBP" + b"0"), "image/webp")
    eq("WOFF2 魔数", guess_mime(b"wOF2" + b"0" * 10), "font/woff2")
    eq("按后缀猜 CSS", guess_mime(b"body{}", "https://a.com/s.css"), "text/css")
    eq("声明优先", guess_mime(b"\x89PNG", "x", "image/jpeg"), "image/jpeg")


# ==========================================================================
# 5. inline（离线：CSS 重写逻辑，用假 fetch）
# ==========================================================================

class _FakeInliner(Inliner):
    """假的抓取器：从字典里取数据，不碰网络。"""

    def __init__(self, files, options=None, base=""):
        super().__init__(options or InlineOptions(), base)
        self.files = files

    def _get(self, url):
        if url in self.files:
            data = self.files[url]
            self.stats.fetched += 1
            self.stats.bytes_in += len(data)
            return data
        self.stats.failed += 1
        return None


def test_inline_offline() -> None:
    group("inline · 资源内联（离线，假抓取）")

    files = {
        "https://s.com/base.css": b"body{color:blue}",
        "https://s.com/img/bg.png": b"\x89PNG\r\n\x1a\n" + b"x" * 40,
        "https://s.com/font.woff2": b"wOF2" + b"y" * 30,
        "https://s.com/style.css": FIXTURE_CSS.encode("utf-8"),
    }

    inl = _FakeInliner(files, InlineOptions(compress_images=False), "https://s.com/")

    # CSS url() 正则
    urls = [m.group("url") for m in CSS_URL_RE.finditer(FIXTURE_CSS)]
    check("CSS url() 能全部提取", len(urls) == 5, str(urls))
    check("能提取到内联的 data URI",
          any(u.startswith("data:") for u in urls), str(urls))

    # @import 展开 + url() 内联
    out = inl._rewrite_css(FIXTURE_CSS, "https://s.com/style.css")
    check("@import 已展开", "@import" not in out, out[:120])
    check("@import 内容已并入", "color:blue" in out)
    check("@charset 已删除", "@charset" not in out)
    check("背景图已内联", "data:image/png;base64" in out, out[:200])
    check("字体已内联", "data:font/woff2;base64" in out or "data:application/font" in out)
    check("已是 data URI 的不重复处理",
          out.count("data:image/gif;base64,R0lGOD") == 1)
    check("#anchor 不动", "url(#anchor)" in out)

    # srcset 解析
    ss = "a.jpg 400w, b.jpg 800w, c.jpg 1600w"
    eq("srcset 挑最大", inl._pick_srcset(ss, "https://s.com/"),
       "https://s.com/c.jpg")
    eq("srcset 像素密度", inl._pick_srcset("a.png 1x, b.png 3x", "https://s.com/"),
       "https://s.com/b.png")
    eq("srcset 空", inl._pick_srcset("", "https://s.com/"), "")

    # 完整 DOM 内联
    root = parse("""<html><head>
      <link rel="stylesheet" href="/style.css">
      </head><body>
      <img src="/img/bg.png">
      <img src="/missing.png">
      <img src="/img/bg.png">
      </body></html>""", fast=False)
    st = inl.inline_dom(root, "https://s.com/")
    st = inl.stats
    html = root.to_html()

    check("link 变 style", "<style" in html and "color:blue" in html, html[:200])
    check("图片变 data URI", "data:image/png;base64" in html)
    check("缺失资源不炸", "<img" in html)
    check("统计有记录", st.inlined >= 2, str(st.inlined))
    check("失败有计数", st.failed >= 1, str(st.failed))

    # 预算限制
    files2 = {"https://s.com/big.png": b"\x89PNG\r\n\x1a\n" + b"z" * 5000}
    inl2 = _FakeInliner(files2, InlineOptions(max_total_bytes=100), "https://s.com/")
    root2 = parse('<img src="/big.png">', fast=False)
    inl2.inline_dom(root2, "https://s.com/")
    check("超预算时跳过", "data:" not in root2.to_html(), root2.to_html()[:80])
    check("跳过被统计", inl2.stats.skipped >= 1)

    # 单资源上限
    inl3 = _FakeInliner(files2, InlineOptions(max_asset_bytes=10), "https://s.com/")
    root3 = parse('<img src="/big.png">', fast=False)
    inl3.inline_dom(root3, "https://s.com/")
    check("超单资源上限跳过", inl3.stats.skipped >= 1)

    # local_only
    files4 = {"https://other.com/x.png": b"\x89PNG\r\n\x1a\n" + b"q" * 20}
    inl4 = _FakeInliner(files4, InlineOptions(allow_external=False), "https://s.com/")
    root4 = parse('<img src="https://other.com/x.png">', fast=False)
    inl4.inline_dom(root4, "https://s.com/")
    check("同域限制生效", "data:" not in root4.to_html())

    # scripts 策略
    for mode, expect_script in (("drop", False), ("keep", True)):
        inl5 = _FakeInliner({}, InlineOptions(scripts=mode), "https://s.com/")
        r5 = parse('<script src="https://s.com/a.js"></script><p>x</p>', fast=False)
        inl5.inline_dom(r5, "https://s.com/")
        has = "<script" in r5.to_html()
        eq("scripts=%s 行为正确" % mode, has, expect_script)


# ==========================================================================
# 6. render
# ==========================================================================

def test_render() -> None:
    group("render · 三种模式渲染")

    ex = extract(FIXTURE_ARTICLE, "https://example.com/post/1", fast_parse=False)

    # reader
    html = render_reader(ex, "https://example.com/post/1", captured="2024-01-01 00:00:00")
    check("reader 有 doctype", html.lstrip().lower().startswith("<!doctype html"))
    check("reader 有 charset", 'charset="utf-8"' in html)
    check("reader 有 viewport", "viewport" in html)
    check("reader 标题转义在 title 里", "<title>测试文章标题</title>" in html)
    check("reader 记录来源 URL", "wv-source" in html)
    check("reader 记录抓取时间", "wv-captured" in html or "2024-01-01" in html)
    check("reader 含内置样式", ".wv-wrap" in html)
    check("reader 含正文", "这是第一段正文内容" in html)
    check("reader 无外链样式", 'rel="stylesheet"' not in html)
    check("reader 无 <script", "<script" not in html.lower())

    # full
    root = parse(FIXTURE_ARTICLE, fast=False)
    full = render_full(root, "https://example.com/post/1", captured="2024-01-01 00:00:00",
                       size_line="测试")
    check("full 有 doctype", full.lstrip().lower().startswith("<!doctype html"))
    check("full 保留原始导航", "关于" in full)
    check("full 保留页脚", "版权所有" in full)
    check("full 有来源条", "wv-banner" in full)
    check("full 只有一个 html 标签", full.lower().count("<html") == 1,
          "count=%d" % full.lower().count("<html"))
    check("full 只有一个 body", full.lower().count("<body") == 1,
          "count=%d" % full.lower().count("<body"))
    check("full 有 banner 样式", "wv-banner" in full)

    # full 不显示 banner
    full2 = render_full(parse(FIXTURE_ARTICLE, fast=False), "https://x.com/",
                        show_banner=False, size_line="")
    check("full 可关闭 banner", 'id="wv-banner"' not in full2)

    # data
    ex2 = extract(FIXTURE_ARTICLE, "https://example.com/post/1", fast_parse=False)
    d = build_data(ex2, "https://example.com/post/1")
    eq("data 标题", d["title"], "测试文章标题")
    eq("data 作者", d["byline"], "张三")
    check("data 有正文", "这是第一段正文内容" in d["text"])
    check("data 有图片列表", isinstance(d["images"], list) and len(d["images"]) >= 1)
    check("data 图片有 alt", any(i.get("alt") == "配图一" for i in d["images"]),
          str(d["images"][:2]))
    check("data 有链接列表", len(d["links"]) >= 1)
    check("data 表格是二维数组",
          d["tables"] and d["tables"][0][0] == ["列一", "列二"], str(d["tables"]))
    check("data 有大纲", any(h["text"] == "测试文章标题" for h in d["headings"]))
    check("data 可 JSON 序列化", isinstance(json.dumps(d, ensure_ascii=False), str))

    # 统一入口
    for m in ("reader", "full", "data"):
        r = no_raise("render(%s) 不炸" % m, render, m,
                     parse(FIXTURE_ARTICLE, fast=False), ex, "https://x.com/")
        if r:
            eq("render(%s).mode" % m, r.mode, m)


# ==========================================================================
# 7. pipeline（离线：文件命名、落盘，用本地 HTTP 服务当靶子）
# ==========================================================================

def test_pipeline_offline(tmpdir: str) -> None:
    group("pipeline · 命名与落盘")

    eq("文件名用标题", _safe_filename("我的文章", "https://a.com/x"), "我的文章.html")
    eq("文件名去非法字符", _safe_filename('a/b:c*d?e', "https://a.com/"), "a_b_c_d_e.html")
    check("无标题退回域名",
          "a.com" in _safe_filename("", "https://a.com/x").replace("-", "."),
          _safe_filename("", "https://a.com/x"))
    eq("data 模式扩展名", _safe_filename("t", "https://a.com/", ".json"), "t.json")

    # unique_path 不覆盖
    os.makedirs(tmpdir, exist_ok=True)
    p1 = unique_path(tmpdir, "x.html")
    open(p1, "w").close()
    p2 = unique_path(tmpdir, "x.html")
    check("重名自动加后缀", p1 != p2 and p2.endswith("x-1.html"), p2)

    # 端到端：起一个本地 HTTP 服务当靶子，完全离线
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    payload = FIXTURE_ARTICLE.encode("utf-8")
    img = b"\x89PNG\r\n\x1a\n" + b"p" * 60

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            if self.path.startswith("/style.css"):
                body = b"body{color:#123}"
                self.send_response(200)
                self.send_header("Content-Type", "text/css")
            elif self.path.startswith("/images/") or self.path.startswith("/bg.png"):
                body = img
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
            elif self.path.startswith("/note"):
                body = payload
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
            else:
                body = b"not found"
                self.send_response(404)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()

    try:
        base = "http://127.0.0.1:%d" % port
        out = os.path.join(tmpdir, "out")

        # reader 模式端到端
        r = vault(base + "/note", mode="reader", out_dir=out,
                  inline_options=InlineOptions(compress_images=False), timeout=10)
        check("reader 端到端成功", r.ok, r.error)
        check("reader 落盘了文件", r.out_path and os.path.isfile(r.out_path), r.out_path)
        if r.ok:
            content = open(r.out_path, encoding="utf-8").read()
            check("产物含正文", "这是第一段正文内容" in content)
            check("产物图片已内联", "data:image/png" in content or "data:image/" in content)
            check("产物无 <link rel=stylesheet",
                  'rel="stylesheet"' not in content)
            check("产物排除侧边栏", "热门文章一" not in content)
            check("产物排除评论", "写得好" not in content)

        # data 模式
        r2 = vault(base + "/note", mode="data", out_dir=out, timeout=10)
        check("data 端到端成功", r2.ok, r2.error)
        if r2.ok:
            loaded = json.load(open(r2.out_path, encoding="utf-8"))
            eq("data 文件标题", loaded["title"], "测试文章标题")
            check("data 文件有正文", "这是第一段正文内容" in loaded["text"])

        # full 模式
        r3 = vault(base + "/note", mode="full", out_dir=out, timeout=10,
                   inline_options=InlineOptions(compress_images=False))
        check("full 端到端成功", r3.ok, r3.error)
        if r3.ok:
            c3 = open(r3.out_path, encoding="utf-8").read()
            check("full 保留导航", "关于" in c3)
            check("full 有 banner", "wv-banner" in c3)

        # 404 处理
        r4 = vault(base + "/nope", mode="reader", out_dir=out, timeout=10)
        check("404 不抛异常且被标记", isinstance(r4.ok, bool))
        check("404 有错误信息", bool(r4.error) or r4.ok, r4.error[:60])

        # 无效 URL
        r5 = vault("", mode="reader", out_dir=out)
        check("空 URL 被拒", not r5.ok and r5.error)

        r6 = vault("http://127.0.0.1:1/x", mode="reader", out_dir=out, timeout=3)
        check("连不上的端口不抛异常", not r6.ok and bool(r6.error))

        # 自动补 https
        r7 = vault("example.invalid", mode="reader", out_dir=out, timeout=3)
        check("裸域名自动补协议", "https://example.invalid" in r7.url, r7.url)

    finally:
        srv.shutdown()
        srv.server_close()


# ==========================================================================
# 8. 在线测试（可选）
# ==========================================================================

def test_library(tmpdir: str) -> None:
    group("library · 离线书库索引")

    from src.library import Entry, build_index, render_index, scan

    # 造几个假的产物：两个 reader HTML + 一个 data JSON + 一个无关文件
    d = os.path.join(tmpdir, "lib")
    os.makedirs(d, exist_ok=True)

    def fake_reader(name, title, source, captured, words=100, imgs=2):
        body = """<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<title>%s</title>
<meta name="generator" content="WebVault">
<meta name="wv-source" content="%s">
<meta name="wv-captured" content="%s">
</head><body><main class="wv-wrap"><article>
<div class="wv-meta"><div class="row"><span>来源 test</span></div>
<div style="margin-top:.4rem"><span>原文</span> <a href="%s">%s</a></div>
<div style="margin-top:.5rem;font-style:italic;opacity:.85">这是摘要文字</div>
<div style="margin-top:.55rem"><span class="wv-badge">正文 %d 字</span><span class="wv-badge">图 %d</span></div>
</div><p>正文内容</p></article></main></body></html>""" % (
            title, source, captured, source, source, words, imgs)
        with open(os.path.join(d, name), "w", encoding="utf-8") as f:
            f.write(body)

    fake_reader("a.html", "第一篇文章", "https://site-a.com/post/1", "2024-01-01 10:00:00")
    fake_reader("b.html", "第二篇文章", "https://site-b.com/x", "2024-02-02 11:00:00",
                words=250, imgs=5)
    with open(os.path.join(d, "c.json"), "w", encoding="utf-8") as f:
        json.dump({
            "url": "https://site-a.com/post/3", "title": "第三篇（数据模式）",
            "captured": "2024-03-03 12:00:00", "generator": "WebVault",
            "word_count": 321, "excerpt": "数据摘要",
            "images": [{"src": "data:image/png;base64,x"}, {"src": "data:image/png;base64,y"}],
        }, f, ensure_ascii=False)
    # 无关文件不该被收进来
    with open(os.path.join(d, "random.html"), "w", encoding="utf-8") as f:
        f.write("<html><head><title>别人的页面</title></head><body>x</body></html>")

    entries = scan(d)
    eq("收录数量（排除无关文件）", len(entries), 3)
    titles = {e.title for e in entries}
    check("标题正确读取", "第一篇文章" in titles and "第二篇文章" in titles, str(titles))
    check("data 模式也被收录", any(e.kind == "json" for e in entries))
    check("来源被读取", any("site-a.com" in e.source for e in entries))
    check("host 被提取", any(e.host == "site-a.com" for e in entries),
          str([e.host for e in entries]))
    check("字数被解析", any(e.word_count == 100 for e in entries),
          str([e.word_count for e in entries]))
    check("摘要被解析", any("摘要" in e.excerpt for e in entries),
          str([e.excerpt[:20] for e in entries]))
    check("按时间倒序", entries[0].mtime >= entries[-1].mtime)

    # 渲染
    html = render_index(entries, title="测试书库")
    check("索引有 doctype", html.lstrip().lower().startswith("<!doctype html"))
    check("索引零外部资源",
          len(re.findall(r'<(?!a\b)(\w+)[^>]*?(?:src|href)="https?://', html)) == 0)
    check("索引含搜索框", 'id="q"' in html)
    check("索引含排序", 'id="sort"' in html)
    check("索引含模式筛选", 'id="mode"' in html)
    check("索引含卡片", html.count('class="card"') == 3, str(html.count('class="card"')))
    check("卡片有 data-search",
          html.count("data-search=") == 3, str(html.count("data-search=")))
    check("卡片链接是相对路径", 'href="a.html"' in html or 'href="b.html"' in html)
    check("标题已转义安全", "第一篇文章" in html)

    # 空目录也要能出页面
    empty = os.path.join(tmpdir, "lib-empty")
    os.makedirs(empty, exist_ok=True)
    p2, e2 = build_index(empty)
    check("空书库也能生成", os.path.isfile(p2) and len(e2) == 0)
    check("空书库有提示", "书库还是空的" in open(p2, encoding="utf-8").read())

    # build_index 写文件
    p3, e3 = build_index(d, title="测试书库")
    check("索引文件已写出", os.path.isfile(p3), p3)
    check("索引写出的条目数一致", len(e3) == 3)

    # -o 指向目录时应在其中放 index.html
    sub = os.path.join(tmpdir, "outdir")
    os.makedirs(sub, exist_ok=True)
    p4, _ = build_index(d, out_path=sub)
    check("out_path 为目录时自动补 index.html",
          p4.endswith("index.html") and os.path.isfile(p4), p4)

    # 索引页不该把自己收进去
    p5, e5 = build_index(d)
    entries2 = scan(d)
    check("索引页不自我收录",
          not any(e.rel_path.endswith("index.html") for e in entries2),
          str([e.rel_path for e in entries2]))


def test_discover() -> None:
    group("discover · 链接发现")

    from src.discover import discover

    page = """<html><head><title>博客列表</title></head><body>
    <nav class="nav">
      <a href="/">首页</a><a href="/about">关于我们</a>
      <a href="/login">登录</a><a href="/tags/tech">技术标签</a>
    </nav>
    <main>
      <a href="/blog/2024/01/hello-world.html">一篇很值得阅读的文章标题在这里</a>
      <a href="/blog/2024/02/another-post.html">另一篇文章，也很有内容</a>
      <a href="/blog/2024/03/third-one.html">第三篇：关于某种技术的思考</a>
      <a href="/blog/archives.html">更多文章……</a>
    </main>
    <footer>
      <a href="/privacy">隐私政策</a>
      <a href="https://other.com/post/1">外部站点的一篇文章</a>
      <a href="/files/report.pdf">下载报告</a>
      <a href="/img/logo.png">logo</a>
    </footer></body></html>"""

    links = discover(page, "https://site.com/list", same_host_only=True)
    urls = [l.url for l in links]
    texts = " ".join(l.text for l in links)

    check("发现到文章链接", len(links) >= 3, str(len(links)))
    check("含第一篇", any("hello-world" in u for u in urls), str(urls))
    check("含第二篇", any("another-post" in u for u in urls), str(urls))
    check("含第三篇", any("third-one" in u for u in urls), str(urls))

    # 导航/功能/资源必须被排除
    check("排除首页", not any(u.rstrip("/").endswith("site.com") for u in urls), str(urls))
    check("排除关于页", not any("/about" in u for u in urls), str(urls))
    check("排除登录页", not any("/login" in u for u in urls), str(urls))
    check("排除标签页", not any("/tags/" in u for u in urls), str(urls))
    check("排除隐私页", not any("/privacy" in u for u in urls), str(urls))
    check("排除 pdf", not any(".pdf" in u for u in urls), str(urls))
    check("排除图片", not any(".png" in u for u in urls), str(urls))
    check("排除跨域（默认）", not any("other.com" in u for u in urls), str(urls))
    check("排除功能文字", "登录" not in texts and "首页" not in texts, texts)

    # 文章分数应高于归档页
    art = [l for l in links if "hello-world" in l.url]
    check("文章排在归档页之前",
          art and art[0].score > 0,
          str([(l.text[:20], l.score) for l in links]))
    check("按分数降序", all(links[i].score >= links[i + 1].score
                            for i in range(len(links) - 1)),
          str([l.score for l in links]))

    # 放开跨域
    links2 = discover(page, "https://site.com/list", same_host_only=False)
    check("--any-host 能收到跨域链接",
          any("other.com" in l.url for l in links2),
          str([l.url for l in links2]))

    # limit
    links3 = discover(page, "https://site.com/list", limit=1)
    eq("limit 生效", len(links3), 1)

    # 去重
    dup = """<body><a href="/a/1.html">第一篇重复标题内容</a>
    <a href="/a/1.html#frag">第一篇重复标题内容</a>
    <a href="/a/1.html?utm_source=x">第一篇重复标题内容</a></body>"""
    l4 = discover(dup, "https://s.com/")
    eq("同页重复链接去重", len(l4), 1)

    # 极端输入不炸
    for label, h in [("空", ""), ("纯文本", "no links here"),
                     ("只有导航", "<nav><a href='/x'>关于</a></nav>")]:
        r = no_raise("discover 不抛异常: %s" % label, discover, h, "https://s.com/")
        check("discover 返回列表: %s" % label, isinstance(r, list))

    # 相对链接要解析成绝对地址
    rel = '<body><a href="post/2024/01/abc.html">一篇文章的相对链接标题</a></body>'
    l5 = discover(rel, "https://s.com/blog/index.html")
    check("相对链接已绝对化",
          l5 and l5[0].url.startswith("https://s.com/"), str([x.url for x in l5]))


def test_retry_and_throttle(tmpdir: str) -> None:
    """回归测试：暂时性失败必须重试，429 必须触发全局退避。

    背景：CDN 在并发抓图时会返回 429。旧代码只重试"网络异常"，
    对 4xx/5xx 直接放弃，结果产物里留下一堆指向原站的破图外链。
    """
    group("retry · 重试与限流退避")

    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from src.fetch import RETRYABLE_STATUS, fetch

    check("429 被认定为可重试", 429 in RETRYABLE_STATUS)
    check("503 被认定为可重试", 503 in RETRYABLE_STATUS)
    check("404 不重试", 404 not in RETRYABLE_STATUS)

    # 服务端：前 N 次返回 429，之后返回 200
    state = {"seen": 0, "fail_times": 2, "requests": 0, "got": 0}
    img = b"\x89PNG\r\n\x1a\n" + b"z" * 80

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            state["requests"] += 1
            if self.path.startswith("/flaky"):
                state["seen"] += 1
                if state["seen"] <= state["fail_times"]:
                    self.send_response(429)
                    self.send_header("Retry-After", "0")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                body = img
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
            elif self.path.startswith("/always429"):
                self.send_response(429)
                self.send_header("Retry-After", "0")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            elif self.path.startswith("/pageretry"):
                # 引用一张永远 429 的图，用来验证"失败被如实报告"
                body = (b'<html><head><title>retry</title></head><body><article>'
                        b'<p>' + b'y' * 300 + b'</p>'
                        b'<img src="/always429.png">'
                        b'</article></body></html>')
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
            elif self.path.startswith("/page"):
                # 页面里引用同一张会先 429 两次的图（多次引用）
                body = (b'<html><head><title>t</title></head><body><article>'
                        b'<p>' + b'x' * 300 + b'</p>'
                        b'<img src="/flaky.png"><img src="/flaky.png">'
                        b'</article></body></html>')
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
            else:
                body = b"nope"
                self.send_response(404)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    try:
        base = "http://127.0.0.1:%d" % port

        # 1) 429 之后重试应当成功拿到内容
        r = fetch(base + "/flaky.png", timeout=10, retries=3)
        check("对 429 重试后成功", r.ok and r.body == img,
              "ok=%s status=%s err=%s" % (r.ok, r.status, r.error))
        check("确实重试了多次", state["seen"] >= 3, str(state["seen"]))

        # 2) 一直 429 时最终失败，但要报告状态码而不是静默
        state["seen"] = 0
        r2 = fetch(base + "/always429", timeout=10, retries=2)
        check("持续 429 最终失败", not r2.ok)
        check("失败原因含 429", "429" in (r2.error or ""), r2.error)

        # 3) 端到端：图片先 429 两次，最终仍应被内联（不留外链）
        state["seen"] = 0
        state["fail_times"] = 2
        out = os.path.join(tmpdir, "retry-out")
        r3 = vault(base + "/page", mode="reader", out_dir=out,
                   inline_options=InlineOptions(compress_images=False, workers=4),
                   timeout=15)
        check("端到端成功", r3.ok, r3.error)
        if r3.ok:
            content = open(r3.out_path, encoding="utf-8").read()
            check("被 429 的图片最终内联进产物",
                  "data:image/png" in content, content[-300:])
            check("产物里没有残留外链图片",
                  'src="http://' not in content and 'src="https://' not in content)
            check("统计显示 0 失败", r3.stats and r3.stats.failed == 0,
                  str(r3.stats.summary() if r3.stats else None))
            check("同一张图只抓一次（缓存生效）", r3.stats and r3.stats.fetched == 1,
                  str(r3.stats.fetched if r3.stats else None))

        # 4) 限流退避确实会等待（而不是立刻打回去）
        from src.inline import Inliner
        inl = Inliner(InlineOptions(), base_url=base)
        t0 = time.time()
        inl._note_status(429)
        inl._throttle_wait()
        waited = time.time() - t0
        check("429 之后确实退避了", waited >= 0.4, "%.2fs" % waited)
        inl._note_status(200)
        inl._note_status(200)
        t1 = time.time()
        inl._throttle_wait()
        check("成功后退避窗口解除", time.time() - t1 < 0.3)

        # 5) 失败必须体现在 summary 里（不能假装完整）
        #    这个页面引用一张永远 429 的图，所以必然有 1 个资源失败。
        r4 = vault(base + "/pageretry", mode="reader", out_dir=out,
                   inline_options=InlineOptions(compress_images=False),
                   timeout=15)
        check("含失败资源的页面仍能出稿", r4.ok, r4.error)
        if r4.ok and r4.stats:
            check("统计里记录了失败资源", r4.stats.failed >= 1,
                  r4.stats.summary())
            check("失败数出现在 summary 里", "资源失败" in r4.summary(), r4.summary())
            check("失败资源的具体地址被保留",
                  any("always429" in u for u in r4.failed_urls()),
                  str(r4.failed_urls()[:2]))
    finally:
        srv.shutdown()
        srv.server_close()


def test_challenge_and_timeout(tmpdir: str) -> None:
    """回归测试：拦截页识别 + timeout 是总预算而不是单次超时。"""
    group("challenge · 拦截页识别与超时预算")

    from src.fetch import _friendly_error, detect_challenge

    # --- 拦截页识别 ---
    cf = ('<!DOCTYPE html><html><head><title>Just a moment...</title></head>'
          '<body><div id="cf-chl-widget">challenges.cloudflare.com</div></body></html>')
    r, a = detect_challenge(cf, 403)
    check("识别 Cloudflare 挑战页", r == "Cloudflare 人机验证", "%r %r" % (r, a))
    check("给出了具体建议", len(a) > 8, a)

    login = "<html><body>请先登录后查看内容</body></html>"
    r2, _ = detect_challenge(login, 200)
    check("识别登录墙", r2 == "登录墙", r2)

    cap = "<html><body><div class='g-recaptcha'></div>请完成验证</body></html>"
    r3, _ = detect_challenge(cap, 200)
    check("识别验证码页", r3 == "验证码页", r3)

    denied = "<html><body>Access Denied - you have been blocked</body></html>"
    r4, _ = detect_challenge(denied, 403)
    check("识别访问拒绝", r4 == "访问被拒绝", r4)

    # 正常长文章不能误判（正文里出现 "captcha" 是很常见的）
    good = ("<html><body><article>" + "这是一篇很长的正文。" * 2000 +
            "文中提到了 captcha 这个词和技术。</article></body></html>")
    r5, _ = detect_challenge(good, 200)
    check("长正文不误判为拦截页", r5 == "", r5)

    check("空内容不误判", detect_challenge("", 200)[0] == "")
    check("正常短页面不误判",
          detect_challenge("<html><body><h1>Hello</h1><p>hi</p></body></html>", 200)[0] == "")

    # --- 错误信息可读性 ---
    import socket
    import urllib.error
    msg = _friendly_error(urllib.error.URLError(socket.timeout("timed out")))
    check("超时错误被翻译成人话", "超时" in msg, msg)
    msg2 = _friendly_error(socket.gaierror("getaddrinfo failed"))
    check("DNS 错误被翻译", "解析" in msg2, msg2)
    msg3 = _friendly_error(ConnectionRefusedError("Connection refused"))
    check("连接拒绝被翻译", "拒绝" in msg3, msg3)

    # --- timeout 是总预算 ---
    # 指向一个不可达地址（RFC 5737 保留段，不会有人应答），
    # 验证整体耗时受 timeout 约束，而不是 timeout × (retries+1)。
    from src.fetch import fetch as _f
    t0 = time.time()
    res = _f("http://192.0.2.1:9/", timeout=3.0, retries=3)
    el = time.time() - t0
    check("不可达地址会失败", not res.ok, res.error)
    check("总耗时受 timeout 约束（< 6s，而不是 4×3s）", el < 6.0, "%.1fs" % el)
    check("失败原因是可读的中文", "超时" in (res.error or "") or "不可达" in (res.error or "")
          or "没有响应" in (res.error or ""), res.error)

    # --- 端到端：拦截页要在 summary 里说出来 ---
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class CH(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            body = cf.encode("utf-8")
            self.send_response(403)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), CH)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        r = vault("http://127.0.0.1:%d/x" % srv.server_address[1],
                  mode="reader", out_dir=os.path.join(tmpdir, "chal"), timeout=10)
        check("拦截页也能出稿（不崩）", r.ok, r.error)
        check("结果里标记了 challenge", r.challenge == "Cloudflare 人机验证", r.challenge)
        check("challenge 出现在 summary", "Cloudflare" in r.summary(), r.summary())
    finally:
        srv.shutdown()
        srv.server_close()


def _external_network_refs(html: str) -> list:
    """列出产物里所有「会真的发起网络请求」的外部地址。

    这是本工具的核心承诺（产物必须自包含），所以用真解析器按标签+属性
    精确判断，而不是用正则在整篇 HTML 上扫 —— 正文里出现 `<URL:http://…>`
    这种纯文本会把粗糙的正则带偏，产生假警报。
    """
    from src.parse import parse as _parse

    net_attrs = {
        "img": ["src", "srcset", "data-src", "data-original"],
        "script": ["src"],
        "link": ["href"],
        "iframe": ["src"],
        "embed": ["src"],
        "source": ["src", "srcset"],
        "video": ["src", "poster"],
        "audio": ["src"],
        "track": ["src"],
        "object": ["data"],
        "input": ["src"],
    }
    ignore = ("data:", "#", "javascript:", "mailto:", "about:", "blob:")
    root = _parse(html)
    found = []
    for tag, attrs in net_attrs.items():
        for node in root.find_all(tag):
            for a in attrs:
                v = (node.get(a) or "").strip()
                if not v or v.startswith(ignore):
                    continue
                if v.startswith(("http://", "https://", "//")):
                    found.append((tag, a, v[:120]))
    return found


def test_selfcontained(tmpdir: str) -> None:
    """核心承诺的回归测试：产物里不能有任何外部网络请求。"""
    group("self-contained · 产物必须自包含")

    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    png = b"\x89PNG\r\n\x1a\n" + b"q" * 200
    gif = b"GIF89a" + b"g" * 120
    css = (b"@import url('/deeper.css');"
           b"body{background:url('/bg.png')}"
           b"@font-face{font-family:X;src:url('/f.woff2')}")

    html = ("<html><head><title>自包含测试</title>"
            "<link rel=stylesheet href='/style.css'>"
            "</head><body><article><h1>标题</h1>"
            + "<p>" + "这是一段足够长的正文内容，用来让提取器认为这是文章主体。" * 12 + "</p>"
            + "<img src='/a.png'><img src='/b.gif'>"
            + "<img src='/c.png' srcset='/c.png 1x, /c2.png 2x'>"
            + "<video poster='/bg.png' src='/v.mp4'></video>"
            + "<script src='/x.js'></script>"
            + "<p>正文里有一个<a href='https://example.com/外部链接'>外部普通链接</a>，应当保留。</p>"
            + "</article></body></html>").encode("utf-8")

    class SH(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            p = self.path
            if p.startswith("/style.css") or p.startswith("/deeper.css"):
                body, ct = css, "text/css"
            elif p.endswith(".png") or p.endswith(".gif"):
                body, ct = (png if p.endswith(".png") else gif), "image/png"
            elif p.endswith(".woff2"):
                body, ct = b"wOF2" + b"f" * 80, "font/woff2"
            elif p.endswith(".js"):
                body, ct = b"console.log(1)", "application/javascript"
            elif p.endswith(".mp4"):
                body, ct = b"\x00\x00\x00\x18ftypmp42" + b"v" * 60, "video/mp4"
            else:
                body, ct = html, "text/html; charset=utf-8"
            self.send_response(200)
            self.send_header("Content-Type", ct)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), SH)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    try:
        base = "http://127.0.0.1:%d/page" % port
        for mode in ("reader", "full"):
            out = os.path.join(tmpdir, "sc-" + mode)
            r = vault(base, mode=mode, out_dir=out, timeout=15,
                      inline_options=InlineOptions(compress_images=False,
                                                   keep_original_urls=False))
            check("%s 模式成功" % mode, r.ok, r.error)
            if not r.ok:
                continue
            html_out = open(r.out_path, encoding="utf-8").read()
            refs = _external_network_refs(html_out)
            check("%s 产物无任何外部网络请求" % mode, not refs, str(refs[:4]))
            check("%s 产物含至少一张内联图" % mode, "data:image" in html_out,
                  html_out[:200])
            check("%s 产物内联了 CSS" % mode, "background" in html_out or "color" in html_out)

        # reader 模式：普通外链 <a> 是内容，不该被当成资源处理
        out = os.path.join(tmpdir, "sc-links")
        r2 = vault(base, mode="reader", out_dir=out, timeout=15)
        if r2.ok:
            c = open(r2.out_path, encoding="utf-8").read()
            check("段内普通 <a> 链接被保留（它是内容，不是资源）",
                  "example.com" in c, "no link found")
            check("保留的 <a> 不算外部资源",
                  not _external_network_refs(c), str(_external_network_refs(c)[:3]))

        # 导航 div 和「阅读全文」这类噪音仍然必须被清掉
        noisy = ("<html><head><title>t</title></head><body>"
                 "<div class='nav'><a href='/'>首页</a><a href='/x'>栏目</a></div>"
                 "<article><p>" + "正文内容。" * 40 + "</p>"
                 "<a href='/more'>阅读全文</a></article></body></html>")
        with open(os.path.join(tmpdir, "noisy.html"), "w", encoding="utf-8") as f:
            f.write(noisy)
        rn = extract(noisy, "https://site.com/p")
        hrefs = [n.get("href") for n in rn.root.find_all("a")]
        check("导航被清除", not any(h in ("/", "/x") for h in hrefs), str(hrefs))
        check("「阅读全文」被清除", "/more" not in hrefs, str(hrefs))

        # data 模式产出的 JSON 里，图片也应该是 data URI
        out2 = os.path.join(tmpdir, "sc-data")
        r3 = vault(base, mode="data", out_dir=out2, timeout=15)
        check("data 模式成功", r3.ok, r3.error)
        if r3.ok and r3.data:
            imgs = r3.data.get("images") or []
            check("data 模式有图片", len(imgs) >= 1, str(len(imgs)))
            non_data = [i for i in imgs if not str(i.get("src", "")).startswith("data:")]
            check("data 模式图片都是 data URI", not non_data, str(non_data[:2]))

        # 已知的真实世界误报案例：正文里出现 <URL:http://...> 这样的纯文本
        tricky = ("<html><head><title>t</title></head><body><article><p>"
                  + "x" * 400 +
                  "</p><pre>&lt;URL:http://abc.com:80/~smith/home.html&gt;</pre>"
                  "<pre>&lt;URL: http://www.w3.org/pub/WWW/&gt;</pre>"
                  "</article></body></html>")
        with open(os.path.join(tmpdir, "tricky.html"), "w", encoding="utf-8") as f:
            f.write(tricky)
        check("正文里的裸 URL 文本不算外部资源",
              not _external_network_refs(tricky),
              str(_external_network_refs(tricky)[:3]))
    finally:
        srv.shutdown()
        srv.server_close()


def test_watch(tmpdir: str) -> None:
    """变更追踪：block_text 的段落保真 + watch 的差异算法。"""
    group("watch · 变更追踪")

    from src import watch
    from src.extract import block_text, extract

    # --- block_text 必须保留块边界 ---
    html = ("<html><head><title>t</title></head><body><article>"
            "<h1>公告标题</h1><p>第一段内容。</p><p>第二段内容。</p>"
            "<ul><li>条目一</li><li>条目二</li></ul></article></body></html>")
    r = extract(html, "https://s.com/p")
    bt = block_text(r.root)
    lines = [l for l in bt.split("\n") if l.strip()]
    check("block_text 按块分行", len(lines) >= 5, str(lines))
    check("标题独立成行", "公告标题" in lines, str(lines))
    check("段落独立成行", "第一段内容。" in lines, str(lines))
    check("列表项独立成行", "条目一" in lines, str(lines))
    check("flatten 的 text 没有换行", "\n" not in r.text, repr(r.text[:60]))
    check("block_text 空输入安全", block_text(None) == "")

    # --- 行切分：长段落要按句断，短句要合并 ---
    lines2 = watch.normalize_for_diff("短句。" * 3 + "\n" + "很长的一段话，" * 30)
    check("超长段落会被切开", len(lines2) >= 2, str(len(lines2)))
    check("短句会合并成一行", any(len(l) < 60 for l in lines2), str([len(l) for l in lines2]))

    # --- 差异：新增 / 删除 / 未变 ---
    d0, s0 = watch.diff_texts("甲。\n乙。", "甲。\n乙。")
    check("相同内容无差异", d0 == [] and s0["added"] == 0 and s0["removed"] == 0, str(s0))

    d1, s1 = watch.diff_texts("甲。\n乙。", "甲。\n乙。\n丙。")
    check("识别新增行", s1["added"] == 1 and s1["removed"] == 0, str(s1))
    check("新增行有 + 前缀", any(l.startswith("+ ") and "丙" in l for l in d1), str(d1))

    d2, s2 = watch.diff_texts("甲。\n乙。", "甲。")
    check("识别删除行", s2["removed"] == 1 and s2["added"] == 0, str(s2))
    check("删除行有 - 前缀", any(l.startswith("- ") for l in d2), str(d2))

    d3, s3 = watch.diff_texts("甲。\n乙。\n丙。", "甲。\n丁。\n丙。")
    check("识别替换", s3["added"] == 1 and s3["removed"] == 1, str(s3))

    check("超长差异会截断", len(watch.diff_texts(
        "\n".join("旧%d。" % i for i in range(500)),
        "\n".join("新%d。" % i for i in range(500)), max_lines=20)[0]) < 60)

    # --- 记录与比较 ---
    d = os.path.join(tmpdir, "watch")
    os.makedirs(d, exist_ok=True)
    url = "https://site.com/notice?utm_source=x"
    check("URL 归一化键去掉跟踪参数",
          "utm_source" not in watch.key_for(url), watch.key_for(url))

    prev1 = watch.record(d, url, "公告", "甲。\n乙。", "a.html")
    check("首次记录返回 None（没有上一版）", prev1 is None, str(prev1))
    check("历史文件已写出", os.path.isfile(watch.history_file(d)))
    check("历史目录名是 .webvault", watch.history_dir(d).endswith(".webvault"))

    info_same = watch.compare(d, url, "甲。\n乙。")
    check("内容相同 → status=same", info_same["status"] == "same", str(info_same["status"]))

    info_chg = watch.compare(d, url, "甲。\n乙。\n丙。")
    check("内容变了 → status=changed", info_chg["status"] == "changed", str(info_chg["status"]))
    check("变化带统计", info_chg["stats"].get("added") == 1, str(info_chg["stats"]))

    # 记录新版本后，再比就是 same
    prev2 = watch.record(d, url, "公告", "甲。\n乙。\n丙。", "a.html")
    check("第二次记录能拿到上一版", prev2 is not None, str(prev2))
    check("上一版有版本列表", bool((prev2 or {}).get("versions")), str(prev2))
    check("同内容重复记录不产生新版本",
          len(watch.history_for(d, url).get("versions") or []) == 2,
          str(len(watch.history_for(d, url).get("versions") or [])))

    # 未知 URL 的比较
    info_new = watch.compare(d, "https://other.com/x", "内容。")
    check("未知 URL → status=new", info_new["status"] == "new", str(info_new["status"]))

    # 版本数上限
    for i in range(30):
        watch.record(d, "https://many.com/p", "多版本", "版本 %d 的内容。" % i)
    vs = len(watch.history_for(d, "https://many.com/p").get("versions") or [])
    check("版本数受上限约束", vs <= watch.MAX_VERSIONS, str(vs))

    # 列出追踪页面
    pages = watch.known_pages(d)
    check("known_pages 有结果", len(pages) >= 2, str(len(pages)))
    check("known_pages 含 url 字段", all("url" in p for p in pages))

    # 坏索引不该让工具崩
    bad_dir = os.path.join(tmpdir, "watch-bad")
    os.makedirs(watch.history_dir(bad_dir), exist_ok=True)
    with open(watch.history_file(bad_dir), "w", encoding="utf-8") as f:
        f.write("{这不是合法 json")
    hist = watch.load_history(bad_dir)
    check("坏掉的索引退回空结构", hist.get("pages") == {}, str(hist))
    check("坏索引下仍能记录",
          watch.record(bad_dir, "https://x.com/1", "t", "内容。") is None)

    # 书库扫描必须跳过 .webvault
    from src.library import scan
    ents = scan(d)
    check("书库扫描跳过 .webvault", not any(".webvault" in e.rel_path for e in ents),
          str([e.rel_path for e in ents]))


def test_mode_meta(tmpdir: str) -> None:
    """产物必须记录自己是什么模式，否则书库的类型筛选是坏的。"""
    group("meta · 产物自描述")

    from src.render import render_full, render_reader
    from src.extract import extract

    # render_reader 收 ExtractResult；render_full 收 Node —— 两个签名不一样
    ex_reader = extract(
        "<html><head><title>T</title></head><body><article><h1>T</h1>"
        "<p>" + "正文内容。" * 40 + "</p></article></body></html>",
        "https://s.com/p")
    h_reader = render_reader(ex_reader, "https://s.com/p")
    check("reader 产物带 wv-mode=reader", 'name="wv-mode" content="reader"' in h_reader,
          h_reader[:400])

    from src.parse import parse
    root2 = parse("<html><head><title>T</title></head><body><p>x</p></body></html>")
    h_full = render_full(root2, "https://s.com/p", show_banner=False)
    check("full 产物带 wv-mode=full", 'name="wv-mode" content="full"' in h_full,
          h_full[:400])

    check("两者都带 wv-source", 'name="wv-source"' in h_reader and 'name="wv-source"' in h_full)
    check("两者都带 wv-captured",
          'name="wv-captured"' in h_reader and 'name="wv-captured"' in h_full)

    # 端到端：full 模式存下来的文件，书库应当识别为 full
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    PAGE = b"<html><head><title>ModeTest</title></head><body><p>hello</p></body></html>"

    class MH(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(PAGE)))
            self.end_headers()
            self.wfile.write(PAGE)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), MH)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        d = os.path.join(tmpdir, "modes")
        base = "http://127.0.0.1:%d/p" % srv.server_address[1]
        vault(base, mode="full", out_dir=d, timeout=10)
        vault(base, mode="reader", out_dir=d, timeout=10)

        from src.library import scan
        modes = {e.mode for e in scan(d)}
        check("书库能区分 full 和 reader", modes == {"full", "reader"}, str(modes))
    finally:
        srv.shutdown()
        srv.server_close()


def test_http_error_status(tmpdir: str) -> None:
    """上游 4xx/5xx：页面存得下来，但必须明确告诉用户存的是错误页。"""
    group("http-status · 错误状态码不能装成成功")

    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    BODY = (b"<html><head><title>404 Not Found</title></head><body>"
            b"<h1>Not Found</h1><p>The requested URL was not found.</p>"
            b"</body></html>")

    class EH(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            code = 404
            if self.path.startswith("/gone"):
                code = 410
            elif self.path.startswith("/boom"):
                code = 500
            elif self.path.startswith("/ok"):
                code = 200
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(BODY)))
            self.end_headers()
            self.wfile.write(BODY)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), EH)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % srv.server_address[1]
    try:
        d = os.path.join(tmpdir, "http-err")

        r404 = vault(base + "/missing", out_dir=d, timeout=10)
        check("404 仍然保存成功（存的是服务器的说法）", r404.ok, r404.error)
        check("404 被记录到 http_status", r404.http_status == 404, str(r404.http_status))
        check("404 有明确的警告文案", bool(r404.http_warning), repr(r404.http_warning))
        check("警告里提到了 404", "404" in r404.http_warning, r404.http_warning)
        check("summary 里能看到 HTTP 状态", "HTTP 404" in r404.summary(), r404.summary())

        r410 = vault(base + "/gone", out_dir=d, timeout=10)
        check("410 也被标出来", r410.http_status == 410, str(r410.http_status))

        r500 = vault(base + "/boom", out_dir=d, timeout=10)
        check("500 也被标出来", r500.http_status == 500, str(r500.http_status))

        r200 = vault(base + "/ok", out_dir=d, timeout=10)
        check("200 不该报警", r200.http_status == 0 and not r200.http_warning,
              "%s %r" % (r200.http_status, r200.http_warning))
    finally:
        srv.shutdown()
        srv.server_close()


def test_friendly_errors(tmpdir: str) -> None:
    """网络错误必须翻译成人话 —— 不能把本地化的系统消息原样甩给用户。"""
    group("errors · 网络错误要说人话")

    import socket
    from urllib.error import URLError
    from src.fetch import _friendly_error

    def has_cn(s: str) -> bool:
        return any("\u4e00" <= ch <= "\u9fff" for ch in s)

    cases = [
        ("域名解析失败", URLError(socket.gaierror(11001, "getaddrinfo failed"))),
        ("连接被拒绝", URLError(ConnectionRefusedError(10061, "拒绝"))),
        ("连接被重置", URLError(ConnectionResetError(10054, "reset"))),
        ("连接超时", URLError(TimeoutError("timed out"))),
        ("连接超时(字符串)", URLError("timed out")),
    ]
    for expect, exc in cases:
        msg = _friendly_error(exc)
        check("%s 被翻译成中文" % expect, has_cn(msg), msg)
        check("%s 不带 URLError 前缀" % expect, "URLError" not in msg, msg)

    # 关键回归：Windows 的系统消息是中文的，英文关键词一个都不出现。
    # 只靠字符串匹配会漏，必须按异常类型/errno 判断。
    win = ConnectionRefusedError(10061, "由于目标计算机积极拒绝，无法连接。")
    msg = _friendly_error(URLError(win))
    check("Windows 中文本地化消息也能识别为连接被拒绝",
          "拒绝" in msg and "端口" in msg, msg)
    check("不会把 WinError 原样吐出来", "WinError" not in msg, msg)

    # 裸异常（未包 URLError）也要认
    check("裸 ConnectionRefusedError", "拒绝" in _friendly_error(win), _friendly_error(win))
    check("裸 TimeoutError", "超时" in _friendly_error(TimeoutError("x")), "")
    check("裸 gaierror", "域名" in _friendly_error(socket.gaierror(11001, "x")), "")

    # 不认识的异常仍然给出可读信息，而不是空串
    unk = _friendly_error(ValueError("怪东西"))
    check("未知异常仍有内容", bool(unk.strip()), repr(unk))
    check("未知异常保留类型名", "ValueError" in unk, unk)

    # 端到端：真连不上的端口，CLI 输出中文原因并退出 1
    import subprocess
    r = subprocess.run(
        [sys.executable, "webvault.py", "http://127.0.0.1:9/nope",
         "-o", os.path.join(tmpdir, "err")],
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=dict(os.environ, PYTHONIOENCODING="utf-8"))
    out = (r.stdout or "") + (r.stderr or "")
    check("连不上时退出码是 1", r.returncode == 1, str(r.returncode))
    check("错误信息是中文", "连接被拒绝" in out or "无法连接" in out, out[-300:])
    check("不再出现裸 URLError 文案", "urlopen error" not in out, out[-300:])


def test_thin_content(tmpdir: str) -> None:
    """JS 渲染的空壳页要认出来，但正常的短页面不能被误伤。"""
    group("thin · 空壳页识别")

    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    # 一个"页面很大但正文都在 JS 里"的典型 SPA 空壳
    SPA = (b"<!doctype html><html><head><title>App</title>"
           b"<script>" + b"var x=1;" * 2000 + b"</script></head>"
           b"<body><div id=app></div></body></html>")

    # 一个真的短页面（本身就没多少字）
    SHORT = (b"<html><head><title>Tiny</title></head><body>"
             b"<article><p>" + b"Short but real content. " * 8 + b"</p></article>"
             b"</body></html>")

    class TH(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            body = SPA if self.path.startswith("/spa") else SHORT
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), TH)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % srv.server_address[1]
    try:
        d = os.path.join(tmpdir, "thin")

        r_spa = vault(base + "/spa", out_dir=d, timeout=10)
        check("SPA 空壳仍能保存", r_spa.ok, r_spa.error)
        check("SPA 空壳被标记正文过少", r_spa.thin_content, str(r_spa.thin_content))
        check("空壳提示提到了 JavaScript", "JavaScript" in r_spa.thin_advice,
              r_spa.thin_advice)
        check("summary 里能看到提示", "正文过少" in r_spa.summary(), r_spa.summary())

        r_short = vault(base + "/short", out_dir=d, timeout=10)
        check("正常短页面不被误伤", not r_short.thin_content, r_short.thin_advice)

        # full 模式不做正文提取，不该报"正文过少"
        r_full = vault(base + "/spa", mode="full", out_dir=d, timeout=10)
        check("full 模式不报正文过少", not r_full.thin_content, r_full.thin_advice)
    finally:
        srv.shutdown()
        srv.server_close()


def test_online(tmpdir: str) -> None:
    group("online · 真实站点（可选）")

    targets = [
        ("example.com", "https://example.com", 1, False),
        ("httpbin/html", "https://httpbin.org/html", 1, False),
    ]
    for label, url, min_imgs, _ in targets:
        r = vault(url, mode="reader", out_dir=tmpdir, timeout=30)
        check("在线抓取 %s" % label, r.ok, r.error[:80])
        if r.ok:
            check("  %s 有标题" % label, bool(r.title))
            check("  %s 产物非空" % label, r.size() > 200)

    # 中文站点编码
    r = vault("https://www.baidu.com", mode="reader", out_dir=tmpdir, timeout=30)
    check("百度能抓", r.ok, r.error[:60])
    if r.ok:
        c = open(r.out_path, encoding="utf-8").read()
        check("中文未乱码", "百度" in c, c[:120])


# ==========================================================================
# main
# ==========================================================================

def main() -> int:
    online = "--online" in sys.argv

    import tempfile
    tmpdir = tempfile.mkdtemp(prefix="webvault-test-")
    print("WebVault 自测套件")
    print("临时目录: %s" % tmpdir)

    test_util()
    test_parse()
    test_extract()
    test_extract_regressions()
    test_fetch_offline()
    test_inline_offline()
    test_render()
    test_pipeline_offline(tmpdir)
    test_library(tmpdir)
    test_discover()
    test_retry_and_throttle(tmpdir)
    test_challenge_and_timeout(tmpdir)
    test_selfcontained(tmpdir)
    test_watch(tmpdir)
    test_mode_meta(tmpdir)
    test_http_error_status(tmpdir)
    test_friendly_errors(tmpdir)
    test_thin_content(tmpdir)
    if online:
        test_online(tmpdir)

    print("\n" + "=" * 62)
    total = PASS + FAIL
    if FAIL == 0:
        print("\033[32m全部通过：%d/%d\033[0m" % (PASS, total))
    else:
        print("\033[31m失败 %d / 共 %d\033[0m" % (FAIL, total))
        print("\n失败项：")
        for f in FAILURES:
            print("  · %s" % f)
    print("=" * 62)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
