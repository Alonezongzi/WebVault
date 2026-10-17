"""渲染 —— 把处理过的 DOM 变成最终的单文件 HTML。

三种模式：
  full    完整快照。保留原始结构与样式，只做资源内联。用于存档。
  reader  正文模式。抽出主体，套一层干净排版，去掉所有站点样式。用于阅读/分享。
  data    结构化提取。不产出页面，产出 JSON（标题/正文/图片/链接/表格）。
          用于喂给后续的爬虫/分析流程。

单文件保证：
  - 所有 CSS 内联
  - 所有图片/字体内联为 data URI
  - 不引用任何外部资源（脚本默认保留会打破这一点，UI 上会说明）
  - 附带内嵌的元信息（来源 URL、抓取时间、原标题）
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

from .extract import ExtractResult
from .parse import Node, VOID_ELEMENTS
from .util import collapse_ws, escape, human_size, normalize_text, unescape

# --------------------------------------------------------------------------
# 阅读模式的排版样式 —— 手写，不依赖任何 CDN
# --------------------------------------------------------------------------

READER_CSS = """
:root{
  --wv-fg:#1a1a1a; --wv-fg-soft:#5a5a5a; --wv-bg:#ffffff;
  --wv-accent:#2b6cb0; --wv-border:#e4e4e7; --wv-code-bg:#f6f8fa;
  --wv-max:44rem;
}
@media (prefers-color-scheme: dark){
  :root{ --wv-fg:#e8e8ea; --wv-fg-soft:#a1a1aa; --wv-bg:#16161a;
         --wv-accent:#7aa2f7; --wv-border:#2e2e35; --wv-code-bg:#1e1e24; }
}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{
  margin:0; background:var(--wv-bg); color:var(--wv-fg);
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC",
    "Hiragino Sans GB","Microsoft YaHei","Source Han Sans SC",sans-serif;
  font-size:17px; line-height:1.75; padding:0 1.1rem 5rem;
}
.wv-wrap{max-width:var(--wv-max); margin:0 auto; padding-top:3rem}
.wv-meta{
  border-bottom:1px solid var(--wv-border); padding-bottom:1.1rem; margin-bottom:2.2rem;
  font-size:.82rem; color:var(--wv-fg-soft); line-height:1.6;
}
.wv-meta a{color:var(--wv-accent); text-decoration:none; word-break:break-all}
.wv-meta a:hover{text-decoration:underline}
.wv-meta .row{display:flex; flex-wrap:wrap; gap:.35rem 1.1rem}
.wv-meta .k{opacity:.7}
.wv-badge{
  display:inline-block; padding:.12rem .5rem; border:1px solid var(--wv-border);
  border-radius:99px; font-size:.72rem; color:var(--wv-fg-soft); margin-right:.35rem;
}
h1,h2,h3,h4,h5,h6{line-height:1.3; margin:2.1em 0 .7em; font-weight:650}
h1{font-size:1.85em; margin-top:0}
h2{font-size:1.45em; border-bottom:1px solid var(--wv-border); padding-bottom:.25em}
h3{font-size:1.2em}
h4{font-size:1.05em}
p{margin:0 0 1.15em}
a{color:var(--wv-accent); text-decoration:none}
a:hover{text-decoration:underline}
img,video,canvas,svg{max-width:100%; height:auto; display:block; margin:1.4em auto;
  border-radius:6px}
figure{margin:1.6em 0}
figcaption{font-size:.84em; color:var(--wv-fg-soft); text-align:center; margin-top:.5em}
blockquote{
  margin:1.4em 0; padding:.1em 1.2em; border-left:3px solid var(--wv-border);
  color:var(--wv-fg-soft);
}
pre{
  background:var(--wv-code-bg); padding:1em 1.1em; border-radius:8px; overflow-x:auto;
  font-size:.86em; line-height:1.6; border:1px solid var(--wv-border);
}
code{
  font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,"Courier New",monospace;
  background:var(--wv-code-bg); padding:.14em .38em; border-radius:4px; font-size:.9em;
}
pre code{background:none; padding:0; font-size:1em}
table{
  border-collapse:collapse; width:100%; margin:1.4em 0; font-size:.9em;
  display:block; overflow-x:auto;
}
th,td{border:1px solid var(--wv-border); padding:.5em .7em; text-align:left}
th{background:var(--wv-code-bg); font-weight:650}
hr{border:0; border-top:1px solid var(--wv-border); margin:2.4em 0}
ul,ol{padding-left:1.6em; margin:0 0 1.15em}
li{margin:.3em 0}
.wv-foot{
  margin-top:4rem; padding-top:1.2rem; border-top:1px solid var(--wv-border);
  font-size:.78rem; color:var(--wv-fg-soft); text-align:center;
}
""".strip()

# 阅读模式的骨架模板
READER_TEMPLATE = """<!DOCTYPE html>
<html lang="{lang}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<meta name="generator" content="WebVault">
<meta name="wv-source" content="{source}">
<meta name="wv-captured" content="{captured}">
<meta name="wv-mode" content="reader">
<style>
{wv_css}
{site_css}
</style>
</head>
<body>
<main class="wv-wrap">
<article>
{meta_block}
{content}
</article>
<footer class="wv-foot">{footer}</footer>
</main>
</body>
</html>
"""

FULL_TEMPLATE = """<!DOCTYPE html>
{doctype}
{html_open}
<head>
<meta charset="utf-8">
{head_content}
<meta name="generator" content="WebVault">
<meta name="wv-source" content="{source}">
<meta name="wv-captured" content="{captured}">
<meta name="wv-mode" content="full">
{wv_extra_css}
</head>
{body_open}
{body_content}
{wv_banner}
{close_tags}
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S %z")


def _detect_lang(root: Node) -> str:
    html = root.find("html")
    if html is not None:
        lang = html.get("lang") or html.get("xml:lang")
        if lang:
            return lang.split("-")[0].split("_")[0] or "zh"
    return "zh"


def _format_meta_block(ex: ExtractResult, source_url: str, captured: str,
                       stats_line: str = "") -> str:
    """阅读模式顶部的来源信息块。"""
    parts = ['<div class="wv-meta">']
    row = []
    if ex.site_name:
        row.append('<span><span class="k">来源</span> %s</span>' % escape(ex.site_name))
    if ex.byline:
        row.append('<span><span class="k">作者</span> %s</span>' % escape(ex.byline))
    row.append('<span><span class="k">抓取</span> %s</span>' % escape(captured))
    parts.append('<div class="row">%s</div>' % "".join(row))
    if source_url:
        parts.append(
            '<div style="margin-top:.4rem"><span class="k">原文</span> '
            '<a href="%s" rel="noopener noreferrer">%s</a></div>'
            % (escape(source_url), escape(source_url))
        )
    if ex.excerpt:
        parts.append(
            '<div style="margin-top:.5rem;font-style:italic;opacity:.85">%s</div>'
            % escape(ex.excerpt[:280])
        )
    badges = []
    badges.append('<span class="wv-badge">正文 %d 字</span>' % ex.word_count)
    if ex.image_count:
        badges.append('<span class="wv-badge">图 %d</span>' % ex.image_count)
    if ex.confidence:
        badges.append('<span class="wv-badge">置信度 %.0f%%</span>' % (ex.confidence * 100))
    if stats_line:
        badges.append('<span class="wv-badge">%s</span>' % escape(stats_line))
    parts.append('<div style="margin-top:.55rem">%s</div>' % "".join(badges))
    parts.append("</div>")
    return "".join(parts)


def _extract_style_texts(ex: ExtractResult) -> str:
    """取回原站样式。

    这些样式是提取阶段用 keep_styles=True 收集的（见 extract.strip_junk）。
    外链样式表在渲染时会被内联阶段抓取；这里只负责把 <style> 原文拼起来。
    """
    styles = getattr(ex, "styles", None) or {}
    chunks = []
    for t in styles.get("inline") or []:
        if t and t.strip():
            chunks.append(t)
    return "\n".join(chunks)


def collect_stylesheet_urls(ex: ExtractResult, base_url: str) -> List[str]:
    """取出需要抓取的外链样式表地址（保留原站样式时用）。"""
    from .util import is_fetchable, safe_urljoin
    styles = getattr(ex, "styles", None) or {}
    out = []
    for href in styles.get("links") or []:
        absu = safe_urljoin(base_url, href)
        if is_fetchable(absu):
            out.append(absu)
    return out


def render_reader(ex: ExtractResult, source_url: str = "", *,
                  keep_site_css: bool = False, captured: str = "",
                  stats_line: str = "") -> str:
    """正文阅读模式：干净排版 + 内联资源。

    注意：优先从 ex.root（活的 DOM）序列化，而不是 ex.html（提取时的快照）。
    内联阶段改的是 DOM，用旧的字符串快照会导致图片/CSS 的内联结果丢失。
    """
    captured = captured or _now_iso()
    if ex.root is not None:
        content = ex.root.to_html()
    else:
        content = ex.html or ""
    title = ex.title or "未命名页面"

    # 去掉正文里残留的标题重复（h1 和 title 一样时保留，它是文章标题）
    site_css = ""
    if keep_site_css:
        site_css = _extract_style_texts(ex)
        # 外链样式表如果已被内联阶段抓成 <style>，这里也一并带上
        if ex.root is not None:
            for s in ex.root.find_all("style"):
                # own_text() 而非 text_content()：后者会跳过 style 的内容
                t = s.own_text()
                if t and t.strip() and t not in site_css:
                    site_css += "\n" + t

    meta = _format_meta_block(ex, source_url, captured, stats_line)
    footer = "由 WebVault 保存于 %s · 单文件离线副本" % escape(captured)

    return READER_TEMPLATE.format(
        lang=_detect_lang(ex.root) if ex.root is not None else "zh",
        title=escape(title),
        source=escape(source_url),
        captured=escape(captured),
        wv_css=READER_CSS,
        site_css=site_css,
        meta_block=meta,
        content=content,
        footer=footer,
    )


# --------------------------------------------------------------------------
# 完整快照模式
# --------------------------------------------------------------------------

BANNER_CSS = """
#wv-banner{
  position:fixed; left:0; right:0; bottom:0; z-index:2147483647;
  background:rgba(22,22,26,.92); color:#e8e8ea; font:12px/1.6 -apple-system,
    BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
  padding:6px 12px; display:flex; gap:14px; flex-wrap:wrap; align-items:center;
  backdrop-filter:blur(8px); border-top:1px solid rgba(255,255,255,.12);
}
#wv-banner a{color:#7aa2f7; text-decoration:none}
#wv-banner a:hover{text-decoration:underline}
#wv-banner .sep{opacity:.35}
#wv-banner .sp{flex:1}
#wv-banner button{
  background:transparent; border:1px solid rgba(255,255,255,.25); color:inherit;
  border-radius:4px; padding:2px 8px; font-size:11px; cursor:pointer;
}
#wv-banner button:hover{background:rgba(255,255,255,.1)}
body{padding-bottom:38px !important}
"""

BANNER_HTML = """
<div id="wv-banner">
  <span>WebVault 离线快照</span><span class="sep">|</span>
  <span>{captured}</span><span class="sep">|</span>
  <a href="{source}" rel="noopener noreferrer">原文 ↗</a>
  <span class="sp"></span>
  <span>{size_line}</span>
  <button onclick="var b=document.getElementById('wv-banner');b.style.display='none';
    document.body.style.paddingBottom='0'">隐藏</button>
</div>
"""


def render_full(root: Node, source_url: str = "", *, captured: str = "",
                show_banner: bool = True, size_line: str = "") -> str:
    """完整快照：保留原站点结构，内联资源，底部加一条来源条。"""
    captured = captured or _now_iso()

    html_el = None
    body_el = None
    head_el = None
    for n in root.iter():
        if n.is_element():
            if n.tag == "html" and html_el is None:
                html_el = n
            elif n.tag == "head" and head_el is None:
                head_el = n
            elif n.tag == "body" and body_el is None:
                body_el = n

    # 没有标准结构 → 包一个
    if body_el is None:
        body_el = Node("body")
        for c in list(root.children):
            if c.is_element() and c.tag in ("html", "head"):
                continue
            c.remove()
            body_el.append(c)
        root.append(body_el)

    if head_el is None:
        head_el = Node("head")
        root.append(head_el)

    # 注入 banner 样式与元信息
    style_node = Node("style")
    tn = Node(kind="text")
    tn.text = BANNER_CSS
    style_node.append(tn)
    head_el.append(style_node)

    for nm, val in (("wv-source", source_url), ("wv-captured", captured),
                    ("wv-mode", "full")):
        m = Node("meta")
        m.set("name", nm)
        m.set("content", val or "")
        head_el.append(m)

    if show_banner:
        banner = Node("div")
        banner.set("id", "wv-banner")
        # 用原始 HTML 塞进去（内容是我们自己生成的，安全）
        from .parse import parse
        frag = parse('<div id="wv-banner">%s</div>' % BANNER_HTML.format(
            captured=escape(captured),
            source=escape(source_url),
            size_line=escape(size_line),
        ))
        holder = frag.find("div")
        if holder is not None:
            for c in list(holder.children):
                c.remove()
                banner.append(c)
        body_el.append(banner)

    html_attrs = ""
    if html_el is not None:
        html_attrs = "".join(
            ' %s="%s"' % (k, v) for k, v in html_el.attrs.items() if k != "lang"
        )
    lang = _detect_lang(root)

    return "<!DOCTYPE html>\n<html lang=\"%s\"%s>\n<head>\n%s\n</head>\n<body%s>\n%s\n</body>\n</html>\n" % (
        lang,
        html_attrs,
        _serialize_children(head_el),
        _serialize_attrs(body_el),
        _serialize_children(body_el),
    )


def _serialize_children(node: Node) -> str:
    return "".join(c.to_html() for c in node.children)


def _serialize_attrs(node: Node) -> str:
    if not node.attrs:
        return ""
    return "".join(
        ' %s="%s"' % (k, str(v).replace('"', "&quot;")) for k, v in node.attrs.items()
    )


# --------------------------------------------------------------------------
# 数据模式
# --------------------------------------------------------------------------

def build_data(ex: ExtractResult, source_url: str = "", *,
               captured: str = "", extra: Optional[Dict] = None) -> Dict:
    """把提取结果组织成结构化 JSON。

    这是给爬虫流程用的：拿到干净正文 + 图片列表 + 链接列表 + 表格，
    直接可以进数据库或喂给 LLM。
    """
    captured = captured or _now_iso()
    data: Dict[str, object] = {
        "url": source_url,
        "final_url": source_url,
        "captured": captured,
        "generator": "WebVault",
        "title": ex.title,
        "byline": ex.byline,
        "excerpt": ex.excerpt,
        "site_name": ex.site_name,
        "lang": "",
        # 用活的 DOM 序列化，保证和内联后的状态一致
        "text": normalize_text(ex.root.text_content()) if ex.root is not None else ex.text,
        "html": ex.root.to_html() if ex.root is not None else ex.html,
        "word_count": ex.word_count,
        "confidence": round(ex.confidence, 3),
        "method": ex.method,
        "images": [],
        "links": [],
        "tables": [],
        "headings": [],
    }

    root = ex.root
    if root is None:
        return data

    data["lang"] = _detect_lang(root)

    # 图片
    imgs = []
    for img in root.find_all("img"):
        src = img.get("src") or ""
        imgs.append({
            "src": src,
            "alt": img.get("alt") or "",
            "title": img.get("title") or "",
            "width": img.get("width") or "",
            "height": img.get("height") or "",
            "inline": src.startswith("data:"),
        })
    data["images"] = imgs

    # 链接
    links = []
    seen = set()
    for a in root.find_all("a"):
        href = a.get("href") or ""
        txt = collapse_ws(unescape(a.text_content()))
        if not href or href.startswith("javascript:"):
            continue
        key = (href, txt)
        if key in seen:
            continue
        seen.add(key)
        links.append({"href": href, "text": txt[:200], "rel": a.get("rel") or ""})
    data["links"] = links

    # 表格 → 二维数组
    tables = []
    for tbl in root.find_all("table"):
        rows = []
        for tr in tbl.find_all("tr"):
            cells = []
            for cell in tr.children:
                if cell.is_element() and cell.tag in ("td", "th"):
                    cells.append(collapse_ws(unescape(cell.text_content())))
            if cells:
                rows.append(cells)
        if rows:
            tables.append(rows)
    data["tables"] = tables

    # 标题层级 → 大纲
    heads = []
    for h in root.find_all(("h1", "h2", "h3", "h4", "h5", "h6")):
        t = collapse_ws(unescape(h.text_content()))
        if t:
            heads.append({"level": int(h.tag[1]), "text": t})
    data["headings"] = heads

    if extra:
        data.update(extra)
    return data


def render_data(ex: ExtractResult, source_url: str = "", *, captured: str = "",
                indent: int = 2, extra: Optional[Dict] = None) -> str:
    return json.dumps(
        build_data(ex, source_url, captured=captured, extra=extra),
        ensure_ascii=False, indent=indent,
    )


# --------------------------------------------------------------------------
# 统一入口
# --------------------------------------------------------------------------

@dataclass
class RenderResult:
    html: str = ""
    data: Optional[Dict] = None
    mode: str = "reader"
    bytes_size: int = 0

    def size_h(self) -> str:
        return human_size(self.bytes_size)


def render(mode: str, root: Node, ex: ExtractResult, source_url: str = "",
           *, captured: str = "", stats_line: str = "", show_banner: bool = True,
           keep_site_css: bool = False) -> RenderResult:
    """按模式渲染。"""
    mode = (mode or "reader").lower()
    captured = captured or _now_iso()

    if mode == "data":
        d = build_data(ex, source_url, captured=captured)
        s = json.dumps(d, ensure_ascii=False, indent=2)
        return RenderResult(html="", data=d, mode="data", bytes_size=len(s.encode("utf-8")))

    if mode == "full":
        html = render_full(root, source_url, captured=captured,
                           show_banner=show_banner, size_line=stats_line)
        return RenderResult(html=html, mode="full", bytes_size=len(html.encode("utf-8")))

    html = render_reader(ex, source_url, captured=captured,
                         stats_line=stats_line, keep_site_css=keep_site_css)
    return RenderResult(html=html, mode="reader", bytes_size=len(html.encode("utf-8")))
